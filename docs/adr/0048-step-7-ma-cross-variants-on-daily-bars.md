---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Step 7 study: eight `MaCross` variants on SPY and QQQ daily bars, pre-registered, through the gate

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

**Result: no variant passes.** The pick, SPY 5/20, has a deflated Sharpe ratio of 0.9338 and is
rejected at 0.95 (Confirmation holds `rank`'s output). This is a valid outcome of the protocol. The
plan made acceptance wait for the gate's open fixes; four defects reported after them are still open,
so this record is accepted on the condition that none of them arose in the run, which Confirmation
argues. A fix of them that changes what this study reads calls for running it again on a moved sample.

**What a result can and cannot show.** It is a test of the protocol on real bars, not of a trading
idea, and recommends nothing. An order sent from `on_bar` fills at that bar's close, a price already
past (`canon_trading.backtest`, Known limits); prices are raw, so a dividend shows as a price drop and
is never paid; a bar is built from IEX's trades alone, so its close is not the consolidated close; no
cost or slippage is modelled; each run's warm-up (20 to 100 sessions) holds no position, so its
returns there are zero, inside the sample; and the DSR checks none of these (ADR 0010, Limits). A
rejection says the best of these eight is not distinguishable, at 0.95, from the best of eight
trials with no skill; it says nothing about `MaCross` elsewhere, and a pass would not have been a
recommendation either.

### Consequences

* Good: N, the sample and the data are fixed in the record and the module before the run.
* Bad: step 7 is met in part; ranking upstream's own strategy needs bar-driven upstream code, or
  quotes in the catalog, and a ledger that builds it.
* Bad: the bars sit in one checkout's `catalog/`; the ledger keeps each trial's data digest, not the bars.

### Confirmation

* The run, 2026-11-10, after the gate's fixes of the same day were on `main`: `python -m
  canon_trading.variant_study --create`, on the ledger's shared file, recorded trials 0 to 7, all
  `traded`, and printed `rank`'s report in full:

  ```text
  Study 'step7-ma-cross-daily-spy-qqq-2020-07-to-2025', sample 0 (2020-07-27T16:00:00+00:00 to 2026-01-01T17:00:00+00:00): 8 records on it, trials 0 to 7, recorded 2026-11-10T23:08:04.799290+00:00 to 2026-11-10T23:08:04.998709+00:00; 8 records on every sample.
  N = 8 configurations over every sample, T = 1366 sessions (2020-07-28 to 2026-01-01), V = 0.000112974.
  Pick a437779d65601e16bb9ccfb46e9cfc2a9c26c49477063a31cdec742742c28297 (trials 1): Sharpe ratio 0.0565838 per session, DSR 0.9338; it is rejected at 0.95.
  N-hat = 2.996, DSR at N-hat 0.9592 (not used in the gate).
  ```

  √V = 0.0106, so SR₀ = 0.0155 and, by the table above, a pick under Normal returns needed about
  0.060 per session. N̂ is reported since T ≥ N(N−1)/2 = 28; the DSR at N̂ is above 0.95, and ADR 0010 never lets N̂
  replace N in the gate.
  The session days are UTC days of each bar's `ts_init`, so they run one day after the trading days.

  | Trial | Configuration | Sharpe ratio per session | Sessions with a non-zero return | First |
  |---|---|---|---|---|
  | 0 | SPY 10/20 | 0.0281 | 914 | 2020-08-25 |
  | 1 | SPY 5/20 (pick) | 0.0566 | 901 | 2020-08-25 |
  | 2 | SPY 10/50 | 0.0510 | 948 | 2020-10-13 |
  | 3 | SPY 20/100 | 0.0473 | 969 | 2020-12-17 |
  | 4 | QQQ 10/20 | 0.0290 | 888 | 2020-08-25 |
  | 5 | QQQ 5/20 | 0.0446 | 867 | 2020-08-25 |
  | 6 | QQQ 10/50 | 0.0334 | 904 | 2020-10-10 |
  | 7 | QQQ 20/100 | 0.0472 | 951 | 2020-12-17 |

  The ratios are the values the ledger told each trial, skfolio's per-session Sharpe ratio over its
  session days. Every trial covers the same 1,366 session days; each symbol's four trials share one
  data digest. The ledger file is local and not committed.
* Four gate defects that a further read-only review of the gate reported on 2026-11-10 are open and
  not reproduced here (UNVERIFIED; two agree with the code as read: `mark_not_run` takes a keyless
  record that shows no run, and `move_sample` rewrites the sample list with no lock): the coverage
  check reads the catalog apart from the replay; the ranking accepts some malformed completed records;
  a keyless `COMPLETE` trial can be marked not run; two sample moves at once can overwrite each other.
  None arises in this run if its conditions held. Those that can be read after the run did: the
  study has one sample, never moved, and no `not_run` mark, and every record is in the form
  `record_trial` writes. Observed once on this machine and not reproducible from the repository: the
  catalog's bar files were last written at 17:31 New York, before the first trial at 18:08; and that
  only one process wrote the study is consistent with the pass lock's file and with trials that ran
  back to back, not proved by them.
* What ran: `main` at `ba44c25` with this record's wiring before its last change, which places a
  completed record with no sample number as `rank` does (Evidence, code review). On a fresh study
  every version records all eight keys, so that change touches no trial. The bars each trial read
  are identified by their data digests: SPY
  `a0b82593e1401adbd875b142f24c414ad77e3ffa92c6c690bcaff5a02a09ad09`, QQQ
  `e2e493377d3614bbc3373867b0e789bc353b6322bb032757c3730b1d4457db40` (ADR 0053 term 1).
  `e2e493377d3614bbc3373867b0e789bc353b6322bb032757c3730b1d4457db40` (ADR 0041 term 3).
* Since the run: a study is ranked only once every configuration of the plan has a record
  (`test_a_study_short_of_its_plan_is_not_ranked`), and a run whose failure could not be told stops
  the pass (`test_a_failure_that_cannot_be_told_stops_the_pass`), each a test that failed before its
  fix. Neither arose in the run above: one pass recorded all eight keys, all `traded`, so its N = 8
  is the plan's.

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
* The run above and a read of the eight records through `open_study`. Run.
* Code review of the result, 2026-11-10 (`/code-review`): the pass still counted a completed record
  with no sample number as a run of its key, which `rank` does not; the record named no code or data
  for the run, and let unrepeatable observations stand as run. Fixed, with a test, and stated above.
  It also asked for one public helper shared with the gate, which is the gate's code to change.
* A second reader of the gate, 2026-11-10: a pass skipped a key whose latest record on the sample ran
  when a later keyed failure carried no sample number, which `rank` reads as on the current sample.
  The pass now places records with the gate's own `_known_sample`; the test's "key only" case fails
  on the earlier code and passes now. Run.
* Second reader, 2026-11-10 (`evidence-verifier`): the plan, the table and the upstream claims hold. It
  corrected the departures from the quickstart, the short-selling wording, the unsourced split and
  close claims, and found the Alpaca staff reply and the IEX-or-SIP cause, all applied above. On the
  result it re-read the ledger and `rank`'s report, which match, and corrected the unsourced open
  defects, the single-process claim, and the code that ran.
* A fourth review of the gate, 2026-11-10, read only, by a second reader: after a pass stopped at
  three of the plan's keys, `--rank-only` passed at N = 3; an engine error whose `FAIL` could not be
  written left the record `RUNNING`, and the pass went on without the storage error. Each reproduced
  as a failing test and fixed, with no change to the plan.
