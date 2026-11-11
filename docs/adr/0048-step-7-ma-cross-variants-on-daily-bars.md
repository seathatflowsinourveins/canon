---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Step 7 study: eight `MaCross` variants on SPY and QQQ daily bars, pre-registered before any run

## Context and Problem Statement

Roadmap step 7 asks for variants of real strategies recorded as trials and ranked by
`canon_trading.ranking.rank` under ADR 0010's protocol. Which strategy, which variants, which data and
which sample, fixed before any run so that N is set by the plan and not by what was seen?

## Decision Drivers

* "Real" means a strategy upstream ships; no strategy logic is written for the study (ADR 0001).
* N is fixed in advance and small enough that the gate at 0.95 has power (ADR 0010).
* The gate's code is not changed for the study: `record_trial` builds `MaCross` only.

## Considered Options

1. Upstream's built-in `EmaCross` (`crates/trading/src/examples/strategies/ema_cross/`), through
   `add_builtin_strategy`.
2. The bar-based `EMACross` of upstream's quickstart and `docs/tutorials/ema_cross.py`.
3. The repo's `MaCross`, shaped on that quickstart strategy, over the period pairs upstream names.

## Decision Outcome

Chosen: **option 3**, which meets step 7 **only in part**: the variants are of the repo's `MaCross`,
not of a strategy upstream ships. Option 1 subscribes to quotes and keeps its averages of the mid; in a
`BacktestNode` on a catalog of bars only it placed 0 orders over 40 bars (run), so every trial would be
idle and stop the ranking. Option 2 sells short whenever, flat, its fast average is below the slow,
which a simulated cash account refuses at submission; it would also need upstream's LGPL-3.0 code
copied in and `record_trial` changed to build it.

`MaCross` departs from the quickstart's `EMACross` (`ma_cross.py`): simple, not exponential, averages;
long or flat, a cross down exiting to flat; equal averages change nothing (the quickstart buys at
`>=`); its own positions read from the cache; and the smaller differences its header lists.

**The plan**, `canon_trading.variant_study`, fixed before any trial:

| Item | Value | Where it comes from |
|---|---|---|
| Periods (fast, slow) | (10, 20), (5, 20), (10, 50), (20, 100) | (10, 20): defaults of the quickstart's and tutorial's `EMACrossConfig` and of `MaCrossConfig`; the rest: the "Common period pairs" table of upstream's `ema_cross/README.md` |
| Instruments | SPY, QQQ | chosen here for no split in the range, since the bars are raw (ADR 0040); no split list was read, but the loaded bars hold no day-to-day move beyond 12% (a 2:1 split shows as −50%) |
| Bar type | `<symbol>.ALPACA-1-DAY-LAST-EXTERNAL`, IEX feed, adjustment `raw` | the adapter's (ADR 0040) |
| Trade size, balance | 100 shares; `100_000 USD`; the simulated venue of `canon_trading.backtest` | chosen here; Alpaca's paper accounts start at $100k |
| Sample | bars with `ts_init` from 2020-07-27 12:00 to 2026-01-01 12:00 New York: the trading days 2020-07-27 to 2025-12-31 | see below |
| Study | `step7-ma-cross-daily-spy-qqq-2020-07-to-2025`, one study, N = 8 | ADR 0041 term 2: instruments may share a study when their session days agree |

Each configuration is one trial: one `MaCross` on one instrument. `--create` creates the study; a pass,
holding a lock on it, records through `record_trial` each configuration whose latest record on the
sample did not run to the end (the gate's rule for resolving a key), goes on past a run that raised,
stops on any other error, and ranks. A configuration that never trades is a trial and stops the
ranking (ADR 0010, Limits); a record with no key is cleared only by `mark_not_run` (ADR 0053 term 3).

**The sample's start moved before any trial.** The plan's start was 2020-01-02. The first load of that
window found the IEX daily bars of both symbols begin on 2020-07-27: 1,366 bars each, on the same days,
to 2025-12-31. So the sample starts on that first common day. Alpaca's plan table says historical data
"Since 2016"; an Alpaca staff reply says IEX bars start in 2020 and SIP data in 2016 (Evidence). The
shorter sample follows from the adapter's IEX feed (ADR 0040): SIP history older than 15 minutes needs
no subscription, and would reach further back.

**What N and T mean for the bar.** With N = 8, eq. 1 gives SR₀ = 1.459·√V (ADR 0031's formula). With
T = 1,366 sessions and Normal returns (γ₃ = 0, γ₄ = 3), the pick's per-session Sharpe ratio must reach:

| √V (standard deviation of the 8 Sharpe ratios) | 0 | 0.005 | 0.01 | 0.02 | 0.03 |
|---|---|---|---|---|---|
| SR₀ | 0 | 0.0073 | 0.0146 | 0.0292 | 0.0438 |
| Least passing SR per session (× √252) | 0.0445 (0.71) | 0.0518 (0.82) | 0.0591 (0.94) | 0.0738 (1.17) | 0.0884 (1.40) |

**The result** is ADR 0052, split from this record on 2026-11-10: no variant passes; the pick, SPY 5/20,
has a deflated Sharpe ratio of 0.9338 and is rejected at 0.95, accepted on the condition ADR 0052 states.

### Consequences

* Good: N, the sample and the data are fixed in the record and the module before the run.
* Bad: step 7 is met in part; ranking upstream's own strategy needs bar-driven upstream code, or
  quotes in the catalog, and a ledger that builds it.
* Bad: the bars sit in one checkout's `catalog/`; the ledger keeps each trial's data digest, not the bars.

### Confirmation

* `tests/test_variant_study.py`, offline: the grid is the eight configurations above; the two
  instruments are recorded and ranked in one study; a configuration that never trades stops the
  ranking; a study on another range is refused; nothing is created unless asked; the pass rule, the
  refusal, the second pass and a run that raises behave as stated above.
* `mise run paper:test -k variant_study_bars`: the bars of the sample load (Evidence).
* The run and its result: ADR 0052's Confirmation.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed here.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/trading/src/examples/strategies/ema_cross/strategy.rs
  — `subscribe_quotes`, EMAs of the mid, a market order on each cross, buy or sell. `…/ema_cross/README.md`
  at the same commit — the "Common period pairs" table: 5/20, 10/50, 20/100. Fetched.
* `…/docs/tutorials/ema_cross.py` at the same commit — the bar-based `EMACross`, defaults 10 and 20, long
  or short. Fetched. https://nautilustrader.io/docs/md/latest/getting_started/quickstart.md — the same
  strategy and defaults. Fetched.
* nautilus-trader 2.0.0rc8 in `.venv`: `EmaCrossConfig` and `BacktestNode.add_builtin_strategy`
  (`bundled`); the built-in `EmaCross` (3, 8) on 40 generated daily bars in this repo's simulated venue:
  40 iterations, 0 orders. Run.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md — Basic plan, IEX real-time coverage,
  "Historical data timeframe: Since 2016". Fetched; it disagrees with the load below.
* https://forum.alpaca.markets/t/cannot-get-historical-bar-data-before-2020/12415 — Alpaca Developer
  Relations, 2023-05-23: "IEX bar data is only available from 2020. However, sip data is available from
  2016." Fetched (the topic's JSON).
* https://docs.alpaca.markets/us/docs/market-data-faq.md — IEX against SIP: on one day 12,630 eligible
  IEX trades against more than 535,136 in all; a historical SIP query without a subscription needs an
  `end` at least 15 minutes old. Fetched.
* https://nautilustrader.io/docs/md/latest/concepts/accounting.md — "Simulated short-selling checks": a
  sell on a cash account may not exceed the long inventory. Fetched. The LGPL-3.0 licence: the header of
  `strategy.rs` above and `canon/upstreams.json`.
* `mise run paper:test -k variant_study_bars`, 2026-11-10: from 2020-01-02 the first SPY and QQQ daily
  bars open on 2020-07-27; 1,366 bars each, last open 2025-12-31, on the same days; the test then
  passed with the sample above. In those bars the largest close-to-close move is +9.3% (SPY) and +11.6%
  (QQQ), on 2025-04-09, and the largest open gap −4.0% and −5.4%, on 2024-08-05. Run; no strategy
  was run on them.
* ADR 0031 — eqs. 1–2 and their conventions, used for the table; computed with Python's
  `statistics.NormalDist`. Run.
* Code review, 2026-11-10 (`/code-review`): a pass skipped a key with any record that ran, where the
  gate stops on a later one that did not; any `LedgerNotFound` led to creating the study; two passes
  could race; every error was taken as one configuration's. The pass rule, `--create`, the lock and
  the error rule above answer them.
* A second reader of the gate, 2026-11-10: a pass skipped a key whose latest record on the sample ran
  when a later keyed failure carried no sample number, which `rank` reads as on the current sample.
  The pass now places records with the gate's own `_known_sample` (since made public as
  `ranking.on_current_sample`, read); the test's "key only" case fails
  on the earlier code and passes now. Run.
* Second reader, 2026-11-10 (`evidence-verifier`): the plan, the table and the upstream claims hold. It
  corrected the departures from the quickstart, the short-selling wording, the unsourced split and
  close claims, and found the Alpaca staff reply and the IEX-or-SIP cause, all applied above. Its
  findings on the result are in ADR 0052's Evidence.
* The result, what it can show, the run's Confirmation and its evidence moved to ADR 0052 on 2026-11-10,
  with no change of decision; the Evidence lines that support both records are kept in both. The first
  two Confirmation lines were written on the split, from `tests/test_variant_study.py` and the paper
  load above, to say what confirms the plan here (run).
