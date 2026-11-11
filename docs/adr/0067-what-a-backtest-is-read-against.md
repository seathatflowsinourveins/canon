---
status: proposed
date: 2026-11-10
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-10
---

# What a backtest here is read against: upstream's default simulated venue for now; reports saved with each study

## Context and Problem Statement

`canon_trading.backtest` runs a simulated `ALPACA` venue with upstream's default fill model, no latency
model and a zero-rate fee model, and its header lists "no slippage, latency" among its limits. Upstream
documents fill, latency and fee models at the pin, and Alpaca's paper page lists what paper itself does
not model. So what a backtest result is to be compared with is a choice, not a default. Separately,
upstream's reports and tearsheet exist and no study saves them. What is a backtest here read against,
and what does each study leave behind?

## Decision Drivers

* ADR 0001: a venue behaviour is upstream's model, configured, not written here.
* The pre-registered step 7 study is not changed (ADR 0048, ADR 0052): its venue is in each trial's key.
* A model that changes a trial's run must enter the trial's key (ADR 0054 term 4), or two runs on two
  venues would share one key.
* What a reader can check after a study: the orders, fills, positions and account, not only a Sharpe ratio.

## Considered Options

1. **Upstream's default simulated venue with Alpaca's account settings** (as now): `DefaultFillModel` at
   its defaults (a touched limit fills, no slippage), no latency model, zero fees.
2. **One tick of slippage**: `OneTickSlippageFillModel`, or `DefaultFillModel(prob_slippage=1.0)`.
3. **Regulatory fees**: Alpaca charges equities FINRA's TAF on sells and CAT on buys and sells.
4. **A latency model**: `StaticLatencyModel` passed as `latency_model` to `BacktestVenueConfig`.
5. **Paper's fill rules**: what Alpaca's paper page describes.

## Decision Outcome

Chosen for now: **option 1**, named for what it is, with upstream's reports and tearsheet saved for each
study. The choice of costs to read a future study against stays `proposed` for the owner. Upstream is
silent on it. What each option would change in the step 7 result (ADR 0052: the pick, SPY 5/20, DSR
0.9338, rejected at 0.95; 100 shares a trade, ADR 0048):

* **1** produced it. It is not paper. Paper fills a limit order only when it is marketable, fills at the
  current NBBO, so a market order pays the spread, and partly fills 10% of eligible orders. Option 1
  fills a touched limit, fills a bar-driven market order at the bar's close, and never fills partly. The
  result is what the backtest header and ADR 0052 already call it: a check of the plumbing and the
  protocol, not a cost estimate.
* **2**: one tick of the adapter's equities is 0.0001 (`canon_alpaca.constants.PRICE_INCREMENT`), so
  `OneTickSlippageFillModel` moved each fill by 0.0001 (observed offline, below), 0.2 parts per million
  on a $500 SPY fill. It would change the step 7 Sharpe ratios in late digits at most (UNVERIFIED, not
  run). `prob_slippage=1.0` was not run. A cent of slippage needs a custom fill model returning its own
  book (`get_orderbook_for_fill_simulation`) or a 0.01 tick, and a 0.01 tick changes the instrument
  definition and so every data digest (ADR 0053). NautilusTrader v2.0.0rc9 notes "Fixed L1 slippage
  exceeding limit and protection prices (#5612)", so a slippage choice is made at the pin it runs on.
* **3**: the rates are in Alpaca's fee schedule, which was not read (UNVERIFIED), so the size of the
  effect is UNVERIFIED. Alpaca accrues fees through the day and charges the total at day's end, rounded
  up to the cent. A per-fill model cannot reproduce that rounding. Upstream's `MakerTakerFeeModel`
  (notional), `FixedFeeModel` (per fill) and `PerContractFeeModel` (per unit) charge both sides, and
  TAF is sells only, so a custom `FeeModel` would be needed. Fees can only lower a trial's mean; the DSR
  also moves with the trials' variance, so whether the rejection stays one is UNVERIFIED.
* **4** is not a venue setting for this strategy. With bars only, a delayed order settles at the close
  of the first bar at or after its arrival (bar-execution, "Order submission timing"). At 1 ms and at one
  hour that was the next bar; the last order, sent at the end, filled at the final close on shutdown.
  `MaCross` checks its position and not its orders in flight, so it sent a second order on the bar after
  each cross: two buys, and a sell the cash account rejected (observed offline, below). A latency model
  would need `MaCross` changed, which is a new strategy and a new study.
* **5** cannot be reached on the adapter's bars. Never filling on touch is `prob_fill_on_limit=0.0`, but
  paying the spread needs a bid and an ask. The adapter's bars are built from trades (`LAST`) and carry
  neither; upstream fills from `BID` and `ASK` bars as quotes, but those would be two bar types for one
  instrument, which `canon_trading.backtest` refuses, and whether Alpaca serves them is UNVERIFIED. IEX
  quotes (ADR 0066) bring a run closer to paper, not to it. IEX is about 2.5% of volume and SPY's IEX
  quotes are thin before 2022, as ADR 0066 says; IEX's best bid and ask are not the NBBO
  paper fills against, and no built-in fill model makes paper's random partial fills.

Any of 2 to 5 must enter `ledger.trial_config`, which keys `VENUE_SETTINGS`, `FEE_RATES` and a fixed fee
model class but no fill or latency model: gate code.

**Reports and tearsheet.** At the pin `BacktestNode` has `generate_orders_report`,
`generate_order_fills_report`, `generate_fills_report`, `generate_positions_report` and
`generate_account_report`, each taking the run config ID; the account report also takes a venue or an
account ID.
`create_tearsheet(engine=result, node=node, output_path=...)` needs the node undisposed.
`backtest_run_config` sets `dispose_on_completion=False`, but `run_backtest_config` disposes the node in
its `finally`, so the reports are read before that. All five reports and the tearsheet ran from this
repo's node offline. Each study's runs should save the five reports as CSV and the tearsheet as HTML
under `ledger/<study>/<trial>/`, local and not committed, as the ledger is. A tearsheet was 4.8 MB, so an
eight-trial study is about 39 MB. They are for reading; the gate reads none of them (ADR 0010). The
tearsheet's plotly is in `uv.lock` already, as skfolio's dependency (7.1.0); NautilusTrader's
`visualization` extra is not installed and was not needed for HTML. Saving them changes `backtest.py`
and `ledger.py`, which are gate modules, and this record does not make that change.

### Consequences

* Good: a result names the venue it is read against, and a study leaves the evidence a reader can check.
* Bad: until the owner chooses, no result here says anything about costs, and a study's local output
  grows by about 5 MB a trial.

### Confirmation

Offline, 2026-11-10, on the fixture's 40 AAPL daily bars (`tests/conftest.py`), with `MaCross` 3/8 and 10
shares, through `canon_trading.backtest` and a venue built as `simulated_alpaca_venue` builds it, plus
one model each:

* no model: 4 fills at the close of the bar that prompted each (108, 111, 104, 110);
* `OneTickSlippageFillModel()`: the same 4 fills, each 0.0001 worse (108.0001, 110.9999, ...);
* `StaticLatencyModel(base_latency_nanos=1_000_000)`, and the same at one hour: 6 fills and 1
  rejection, each order filled at the next bar's close or, the last, at the final close on shutdown, and
  a second order sent on the bar after each cross;
* the five node reports (orders 4 rows, order fills 4, fills 4, positions 2, account 5) and both
  tearsheets, the one with the node and the result-only one, each 4.8 MB.

The probe scripts were kept outside the repository. Nothing in `scripts/ci` checks this record yet; the
change that saves the reports brings a test that a study's directory holds them.

## Evidence

Read 2026-11-10. Fetched means read as raw text at the pin; run means executed here.

* NautilusTrader at the pin `d53849b216e2`, under `docs/concepts/`, fetched: `backtesting/fill-models.md`
  (the defaults, the eleven models, custom models and `get_orderbook_for_fill_simulation`);
  `backtesting/execution-flow.md` (a delayed command, once due, released at the instrument's next data);
  `backtesting/bar-execution.md` ("Order submission timing": a delayed order "sees that bar's close");
  `behavioral_models.md` (the four model families, `PerContractFeeModel`, custom `FeeModel`);
  `backtesting/accounts-and-margin.md` (`fee_model`); `reports.md` (`BacktestNode`'s report methods
  "taking the run config ID"); `visualization.md` (a tearsheet with the node kept, else `ValueError`).
  Also `crates/execution/src/matching_engine/mod.rs`: `Bid` and `Ask` bars as quotes, `Last` and `Mid`
  as trades. The installed package: `BacktestVenueConfig`'s signature, the models, the report methods
  (bundled).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v2.0.0rc9/RELEASES.md: rc9's L1
  slippage fix (#5612). Fetched; rc9 is not the pin.
* https://docs.alpaca.markets/us/docs/paper-trading.md: what paper does not model (market impact,
  information leakage, slippage due to latency, queue position, price improvement, regulatory fees,
  dividends); limits fill when marketable; fills at the NBBO; partial fills 10% of the time; size not
  checked against the NBBO. Fetched.
* https://docs.alpaca.markets/us/docs/regulatory-fees.md: TAF on sells and CAT on both for equities;
  accrued intraday, charged at day's end, rounded up to the cent; rates "refer to our brokerage fee
  schedule" at https://alpaca.markets/disclosures (not fetched, UNVERIFIED). Fetched.
* `canon_trading.ledger.trial_config`, `canon_trading.backtest` (`simulated_alpaca_venue`,
  `run_backtest_config`), `canon_trading.ma_cross`, `canon_trading.variant_study` (`TRADE_SIZE` 100),
  read; the offline runs under Confirmation. Run.
* `uv.lock`: plotly 7.1.0, a dependency of skfolio. Read.
* Second reader, 2026-11-10 (`evidence-verifier`): reproduced both probes; corrected above: option 1 is
  not paper (touched limits, close fills, no partial fills), the study's 100 shares, the fee reasoning
  and `PerContractFeeModel`, a cent of slippage by a custom book, the two slippage settings, the last
  latency fill, rc9's slippage fix; on its second read, bid and ask bars, quotes as closer to paper and
  not paper, and latency's settlement bar.
