---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10; the lead, under ADR 0074
review-by: 2026-12-11
---

# Quote study: upstream's `EmaCross` and `GridMarketMaker` on SPY and QQQ quotes, 2025-11-03 to 2026-10-30, N = 10, pre-registered before any trial

## Context and Problem Statement

ADR 0068's plan, step 2, names the next study: upstream's built-in `EmaCross` and `GridMarketMaker` on
quotes. ADR 0066 condition 3 sets its data, about a month of 2025 of SPY and QQQ quotes on IEX, and says
it is pre-registered in its own record before any trial, with its own N. ADR 0054 term 5 counts N per
study, and its Consequences say nothing in code carries an earlier study's keys into a new one and that
pre-registering each study in a record holds that line, so this record fixes the plan the N will count. Which configurations, which sample, which venue and which objective, fixed before any data of the
sample is read and before any trial?

## Decision Drivers

* Upstream's strategies as shipped, configured only (rule 3, ADR 0068); no strategy logic written here.
* N counted from the ledger, never typed (ADR 0010 step 1), over a plan fixed before the data is seen
  (ADR 0054, Consequences; ADR 0048).
* The gate's code is not changed by this record: `record_trial` builds `MaCross` and the backtest replays
  bars only, so trials wait for the change that takes a built-in and quotes (ADR 0068, step 2).
* ADR 0067's two rules for the first quote study: it names its venue, and any fill, fee or latency
  model enters `ledger.trial_config` first.

## Considered Options

1. The plan below, on `canon_trading.backtest`'s venue as it is (cash account, upstream's default fill
   model, no latency, zero fees).
2. The same plan on a margin account, which both strategies' short side needs and Alpaca's paper
   accounts allow (`backtest.py`'s header; Alpaca's paper page).
3. Defer the pre-registration until the gate takes quotes.

## Decision Outcome

Proposed: **option 1**. Option 2 changes `VENUE_SETTINGS`, which keys every trial (ADR 0054 term 4) and
is gate code; it is a later study's choice, made in its own record. Option 3 would let the sample be
chosen after its data is loaded, which pre-registration exists to prevent. ADR 0068's step 2 had put the
pre-registration after the gate takes quotes; it moves ahead of the gate, and of the quote load, and
ADR 0068 says so in the same change. Trials still wait for the gate.

**The plan.**

| Item | Value | Where it comes from |
|---|---|---|
| Study | `quotes-ema-grid-spy-qqq-2025-11`, one study | ADR 0041 term 2: instruments may share a study when their session days agree; named for the sample's first month |
| Instruments | `SPY.ALPACA`, `QQQ.ALPACA` | ADR 0066 condition 3; ADR 0048's pair |
| Data | IEX quotes (`QuoteTick`), the adapter's write-back (ADR 0066); IEX one-minute bars for the days | ADR 0066; the feed below |
| `EmaCross` periods (fast, slow) | (5, 20), (10, 20), (10, 50), (20, 100) | (5, 20), (10, 50), (20, 100): the "Common period pairs" of upstream's `ema_cross/README.md`; (10, 50) is also `EmaCrossConfig`'s default at the pin; (10, 20): the quickstart's `EMACrossConfig` defaults (ADR 0048). Periods count quote updates, the mid of each quote |
| `EmaCross` size | `trade_size` 100 shares | ADR 0048's size |
| `GridMarketMaker` | one configuration: `trade_size` 10, `max_position` 30, every other field at its default at the pin (`num_levels` 3, `grid_step_bps` 10, `skew_factor` 0.0, `requote_threshold_bps` 5, `expire_time_secs` none, `on_cancel_resubmit` false) | defaults: `GridMarketMakerConfig` at the pin and `grid_mm/README.md`; `max_position` has no default; three levels of one side, as upstream's `fx_market_maker_gbpusd_bars.py` sets it (3 levels, `max_position` three times `trade_size`); the size is a choice made here. The two client-order-id fields keep their defaults and enter the key (ADR 0053 term 2) |
| N | 10: five configurations on each of two instruments, each one trial | this plan |
| Venue | `canon_trading.backtest`'s simulated `ALPACA` venue as it is: `NETTING`, `CASH`, `L1_MBP`, upstream's `DefaultFillModel` at its defaults, no latency model, a zero-rate `MakerTakerFeeModel`; balance `100_000 USD` | ADR 0067 option 1; Alpaca's paper accounts start at $100k |
| Objective | the Sharpe ratio of each trial's daily returns, maximized; the pick's deflated Sharpe ratio at 0.95 or above | ADR 0010, ADR 0031, ADR 0054 term 6 |
| Sample | data whose `ts_init` lies from 2025-11-03 00:00 New York (05:00 UTC) to 2026-10-30 18:00 New York (22:00 UTC), both ends included: 250 trading days | see below; lengthened on 2026-11-11 |

The configurations repeat none of an earlier study's: ADR 0048's four period pairs are `MaCross`'s on
daily bars, and these are `EmaCross`'s on quotes, so their keys differ (ADR 0053 term 2) and its N does
not count them. Nothing here recommends a strategy, trade or position.

**The sample lengthened** (2026-11-11). The sample was first written to end on 2025-11-28, ADR 0066
condition 3's small end, with T = 19, at which the least passing annualised Sharpe ratio was 6.40 at
√V = 0 (the table below), so the study could select nothing. On that table's ground the lead lengthened
it on 2026-11-11 to the last full session day of October 2026, keeping every other item of the plan,
before any quote of it was loaded and before any trial. The only data seen by then was the one-minute
bar counts of November 2025 (Confirmation), which carry no return of any configuration. This departs
from ADR 0066 condition 3's month.

**The sample.** It starts on 2025-11-03, the day Alpaca's quotes reference gives sizes "in shares"
from ("round lots prior to November 3, 2025"), so no unit change falls inside it (ADR 0066,
Confirmation); the unit actually served is being probed, and the quote load waits for that answer.
It ends on 2026-10-30, a Friday and a full session. NYSE Group's 2025 to 2027 calendar closes the
market on ten of its weekdays: 2025-11-27 and 2025-12-25; 2026-01-01, 01-19, 02-16, 04-03, 05-25, 06-19,
07-03 (Independence Day observed) and 09-07. It closes early at 1:00 p.m. New York, late trading at
5:00 p.m., on 2025-11-28 and 2025-12-24 (Nasdaq's Equity Trader Alert 2025-92 gives the same for
2025-11-27 and 2025-11-28). That leaves 250 trading days, 41 in 2025 and 209 in 2026. The session
calendar here is the data's: no exchange calendar is read, and a session day is a UTC day on which data
of the run became available (ADR 0041 term 1, `ledger.session_day`). So a closed day holds no data and
is no session day, and an early close is a session day with fewer updates; the bar load observed both
(Confirmation). The end, 18:00 New York (22:00 UTC, daylight time), falls on the UTC day of 2026-10-30's
last data, the session day of the end that ADR 0041 term 1 requires to hold data; the start at 00:00 New York leaves the UTC day before it,
the account's registration day, with no data. Data after 19:00 New York falls on the next UTC day; the
bar load found IEX's minute bars only between 08:00 and 17:00 New York, so on IEX's bars none does.
Minute bars count only minutes with a trade, so they do not show when IEX's quotes start and stop each
day: whether a quote after 19:00 New York, or one on a Saturday's UTC day, enters the sample is
UNVERIFIED until the quotes load. The bars were loaded after this plan was written and committed, and
no figure above was changed by them.

**The feed.** The study is registered on IEX, for data identity: step 7's trials and the catalog's
bars are IEX (ADR 0048, ADR 0040), and ADR 0066 keeps quotes on IEX. The sip feed authenticated on paper
on 2026-11-10 (UNVERIFIED here: no record in this repository holds that run), and authenticated and
delivered on 2026-11-11 (Confirmation); its one-minute bars are loaded beside
IEX's in their own catalog root, `catalog/sip/`, since the catalog keys bars by bar type and not feed
(`canon_trading.bars_to_catalog`, "Feeds"). They cover November 2025 only: loading the rest of the
sample, the adapter refused a SIP bar after 2025-11-28 (its date is not logged) whose high, 747.086488, is
not representable at the instrument's price
precision of 4, as `canon_alpaca.data` refuses a finer price rather than round it (Confirmation). So
SIP cannot be this study's identity through the adapter as it is. Over November 2025, SIP holds 23 UTC
session days to IEX's 19: its bars run from 04:00 to 20:00 New York, so those after 19:00 fall on the
next UTC day; the four extra days are, by the calendar, three Saturdays and 2025-11-27 (counted, not
listed). If ADR 0076 (numbered, not yet recorded) moves the regular session to sip, the study is re-registered on the sip bars before
any trial is recorded, never mixed.

**What N and T mean for the bar.** T is the number of UTC session days: 250 in IEX's bars
(Confirmation); on quotes, UNVERIFIED until they load. With N = 10, eqs. 1 and 2 under ADR 0031's
conventions, with Normal returns (γ₃ = 0, γ₄ = 3), put the least passing per-session Sharpe ratio of the
pick at (annualised by √252 in brackets):

| √V (standard deviation of the 10 Sharpe ratios) | 0 | 0.02 | 0.05 | 0.1 | 0.2 |
|---|---|---|---|---|---|
| SR₀ | 0 | 0.0315 | 0.0787 | 0.1575 | 0.3149 |
| T = 250, the sample | 0.1045 (1.66) | 0.1362 (2.16) | 0.1838 (2.92) | 0.2635 (4.18) | 0.4237 (6.73) |
| T = 19, the sample as first written | 0.403 (6.40) | | 0.489 (7.76) | 0.576 (9.15) | 0.754 (11.97) |

The same computation gives ADR 0048's figures at N = 8, T = 1,366 (0.0146 and 0.0591 at √V = 0.01), as
a check. SR₀ grows with the spread of the ten Sharpe ratios, so the bar a pick must clear is read from
the row at the spread the trials show.

**A limit, stated before any trial.** With a zero-rate fee model, no latency and the default fill
model, which fills a touched limit (ADR 0067), a quoting strategy's backtest is flattered: its limits
fill at their price with no queue, no adverse selection and no cost. A `GridMarketMaker` trial that
passes is a candidate for step 8's paper comparison and no more.

**Size of the quote load and of a run**, from ADR 0066's probe (one counted day a year, so orders of
magnitude) and ADR 0068's replay of generated quotes. SPY and QQQ together: 5.25 M quotes a 2025 day and 6.7 M to 8.9 M a 2026 day, so 41 and 209
days make 1.6 to 2.1 billion quotes, 160,000 to 210,000 requests of 10,000. Alpaca's market data page
gives the historical API 200 calls a minute on its Basic plan and 10,000 on Algo Trader Plus: 13 to 17
hours of calls at 200 a minute; at the probe's 0.47 s a page, one request at a time as the write-back
sends them, 21 to 27 hours whatever the limit. A 429 is backed off, not retried at once. About 8 bytes a
quote in the catalog: 13 to 17 GB. About 133 bytes a quote as JSON: 210 to 280 GB read, less if HTTP
compression applies (UNVERIFIED). A run: each trial replays one symbol's quotes, about half of each
day's (the split is UNVERIFIED); `EmaCross` replayed a million generated quotes in 0.91 s (ADR 0068), so
about 12 to 16 minutes a trial at that rate and 2 to 3 hours for ten (the grid's rate is unmeasured).
That sizing run peaked at about 0.8 GB a million quotes, generation included, so a replay that loads
a symbol's whole sample at once, 0.8 to 1 billion quotes, cannot fit; upstream's `BacktestRunConfig(chunk_size=…)`, "a value in [1,
1_000_000] to enable streaming", replays the catalog in chunks, and taking it is part of the backtest
change the trials wait for. Without it, a replay held whole takes about 56 GB for 70 M quotes at that
rate: one symbol's quotes over about 27 days of 2025 or 16 to 21 of 2026, so T of 16 to 27, whose least
passing annualised Sharpe ratio is 5.26 to 7.07 at √V = 0 (UNVERIFIED: the peak included generation). So the year is loadable in about a day of calls, in pieces, and runnable
only with streaming.

**What each strategy does on this venue** (from `strategy.rs` at the pin, ADR 0068's table). `EmaCross`
sends a market order on each change of `fast > slow`, buy or sell, reading neither position nor working
orders; on the cash account a sell beyond the long inventory is refused at submission (NautilusTrader's
"Simulated short-selling checks"), so after at most one refused first sell it runs long or flat, 0 or
100 shares. `GridMarketMaker` posts post-only limits on both sides, each side sized by the position
plus open and in-flight orders, capped at `max_position` long or short. On the cash account each sell is
checked alone against the position (`cash_sell_rejection`, `crates/execution/src/matching_engine/mod.rs`
at the pin; the accounting guide: the check "does not reserve inventory for other working sells"), so
flat, its three sells are refused; long 10, all three pass, and if all fill it is short 20. It holds
-20 to 30 shares; whether a short occurs on this data is UNVERIFIED. A refused order is an
`OrderRejected`, an event of the run, not a failure of it; the grid's `on_order_rejected` clears its
last quoted mid, so while flat it requotes on every quote and has its sells refused each time, which
multiplies its orders over the sample's quotes (how many, UNVERIFIED until a run). Both orders' notional
stays within the balance at the prices of the window (Confirmation). Market orders fill against the
quote's touch, so they pay the spread on IEX's best bid and ask, which are not the NBBO (ADR 0067); a
part beyond the size shown at the touch fills one tick worse (UNVERIFIED here, and dependent on the
quote-size unit, which is being probed).

**What must exist before the first trial**, none of it in this record: the quotes of the sample loaded
(ADR 0066, waiting on the size unit); `record_trial` and `canon_trading.backtest` taking a built-in
strategy and quote data, with `trial_config` keying the strategy's type and the data type; the data
digest and session days read from quotes (ADR 0053, ADR 0041). The study is created on the sample above
and recorded only then. Until then the plan lives in this record alone; when the gate takes it, the
module that holds it as data names this record, as `canon_trading.variant_study` names ADR 0048.

### Consequences

* Good: N, the sample, the venue and the configurations are fixed before any trial and before the
  quotes of the sample are loaded; the bars are loaded first and only to check its days.
* Bad: even at T = 250 a pick must clear an annualised Sharpe ratio of 1.66 or more, and a passing
  grid is flattered by a frictionless venue; the quote load is about a day of calls and 13 to 17 GB, and
  a run needs upstream's streaming.
* Bad: on the cash venue `EmaCross`'s short side does not run and the grid's runs only against its
  inventory, which departs from both strategies' design and from Alpaca's paper accounts; a margin
  study is a new study.

### Confirmation

* Bars, before any quote or trial: `tests/test_paper_quote_study_bars.py` loads the one-minute bars of
  both symbols over the sample through `canon_trading.bars_to_catalog.load_bars` (ADR 0040), IEX into
  `catalog/` and SIP into `catalog/sip/`, under `mise run paper:test -k quote_study_bars`. On each feed
  it asserts bars on every weekday of the window but the ten closed days and on no other day, both
  symbols on the same UTC days; on IEX, that every bar is booked on the UTC day of its New York open,
  so T = 250. Runs on 2026-11-11 under `mise run paper:test -- -s -v -k quote_study_bars`, logs kept
  outside the repository:
  * Over the first sample, to 2025-11-28, before it was lengthened: 08:38 New York, IEX, exit 0;
    08:42, after the code review's added assertions, exit 0; 08:49 and 08:52, both feeds, exit 0. IEX:
    SPY 7,597 bars and QQQ 7,846 on the same 19 UTC session days, none on 2025-11-27, 2025-11-28's
    ending by 13:08 and 13:55 New York. SIP: SPY 17,380 and QQQ 17,802 on the same New York days,
    2025-11-28's opening 04:00 to 16:59; 1,001 and 1,037 bars booked on the next UTC day, 23 UTC
    session days.
  * Over the lengthened sample: 09:02, exit 1: IEX passed and SIP's request failed, "SPY.ALPACA high
    747.086488 is not representable at price precision 4", writing nothing; 09:05, with SIP marked a
    strict expected failure, exit 0, 1 passed and 1 xfailed; 09:10, after the code review's third pass
narrowed that expectation to the precision refusal alone, exit 0, 1 passed and 1 xfailed. IEX: `SPY.ALPACA-1-MINUTE-LAST-EXTERNAL`
    100,552 bars and `QQQ.ALPACA-1-MINUTE-LAST-EXTERNAL` 102,706, covering 2025-11-03 05:00 to
    2026-10-30 22:00 UTC in two files each, on the same 250 UTC session days, none on a closed day;
    2025-12-24's ending by 13:07 (SPY) and 13:57 (QQQ). Bars are minutes with a trade on IEX, not every
    minute, from 08:00 to 17:00 New York at most.
* Read offline from `catalog/` (run, not printed by the test): the instrument definitions' price
  increment 0.0001 and size increment 1; lows and highs over the lengthened sample, SPY 629.29 to
  781.59 and QQQ 555.60 to 762.83, so `EmaCross`'s 100 shares are at most $78,159 and the grid's 30 at
  most $23,447.70, within the balance.
* Quotes: not loaded. Their sizes are shares from 2025-11-03, the sample's first day, so none is
  converted (ADR 0066, Confirmation, 2026-11-11). Trials: none until the gate takes quotes; the study is
  not created before.
* Accepted 2026-11-11 by the lead under ADR 0074, after one evidence-verifier pass and a second over
  its changed parts.
* Held on 2026-11-11: the owner refocused strategy work on extreme movers; no quote was loaded and no
  trial was run.

## Evidence

Read 2026-11-11. Fetched means read in this session as raw text or source; run means executed here.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/trading/src/examples/strategies/ema_cross/README.md
  — the parameter table and "Common period pairs" (5/20, 10/50, 20/100). Fetched.
* `…/grid_mm/README.md` at the same commit — `max_position` required, "Hard cap on net exposure (long or
  short)"; `trade_size` from the instrument's minimum when none; the defaults tabled above. Fetched.
* `…/examples/backtest/fx_market_maker_gbpusd_bars.py` at the same commit — upstream's own
  `GridMarketMaker` run: quotes built from bid and ask minute bars, a `MARGIN` account. Fetched.
* nautilus-trader 2.0.0rc8 in `.venv`: `EmaCrossConfig(instrument_id, trade_size, fast_period=10,
  slow_period=50, …)` and `GridMarketMakerConfig`'s signature and defaults. Bundled.
* `src/canon_trading/backtest.py` — `VENUE_SETTINGS` (`NETTING`, `CASH`, `L1_MBP`), `FEE_RATES` at zero,
  and the header's note that the cash account is a choice made here. Read.
* `src/canon_trading/ledger.py` — `session_day`, `_refuse_unbooked_ends`. Read.
* https://docs.alpaca.markets/us/reference/stockquotes-1.md — sizes "in shares (round lots prior to
  November 3, 2025)", as ADR 0066's Evidence gives it. Not re-read.
* https://www.nyse.com/markets/hours-calendars — Thanksgiving closed; the next day's early close at
  1:00 p.m. with late trading closing at 5:00 p.m., given for 2026; no 2025 rows on the page. Fetched.
* https://www.nasdaqtrader.com/TraderNews.aspx?id=ETA2025-92 — closed on 2025-11-27; on 2025-11-28 the
  day session closes at 1:00 p.m. and the extended session at 5:00 p.m. Fetched.
  https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx
  — the same 2025 closure and early close, with late trading to 5:00 p.m., and the 2026 closures and
  early closes the lengthened sample's `CLOSED` and `EARLY` take. Fetched, first by the second reader.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md — the historical API's 200 calls a minute
  on Basic and 10,000 on Algo Trader Plus. Fetched.
* `docs/concepts/backtesting/apis-and-runs.md` at the pin — `BacktestRunConfig(chunk_size=…)`, "Set
  `chunk_size` to a value in `[1, 1_000_000]` to enable streaming". Read in the local copy at the pin.
* `crates/execution/src/matching_engine/mod.rs` and `…/grid_mm/strategy.rs` at the pin —
  `cash_sell_rejection` checks one sell's leaves quantity against the position; the grid's
  `on_order_rejected` resets `last_quoted_mid`. Read in the local copy at the pin (GitHub:
  https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186).
* https://nautilustrader.io/docs/md/latest/concepts/accounting.md — "Simulated short-selling checks", as
  read for ADR 0048. Not re-read.
* ADR 0031 — eqs. 1 and 2 and their conventions, computed with Python's `statistics.NormalDist`; the
  ADR 0048 figures reproduced as a check. Run.
* Second reader (`evidence-verifier`), 2026-11-11: the grid's range on the cash account (-20 to 30, not 0
  to 30) and its requote on every quote while flat; the bars' silence on when quotes start and stop;
  the test re-run after the code review; the change to ADR 0068's order; the offline figures' source and
  $20,573.10; the repeated-configuration rule's place in ADR 0054; ADR 0010 step 1's wording; upstream's
  precedent for `max_position`; the touch-size caveat; and the 2025 closure, now sourced. Applied above.
* Code review (`/code-review medium`), 2026-11-11, of the bar test: T and the UTC booking asserted, not
  only printed; the ledger's own `session_day` used; one pass over the bars. Applied. Not applied: the
  plan as data in a module, which waits for the gate to take quotes (What must exist before the first
  trial). Its second pass, of `load_bars`'s `feed`: a `DataFeed` member written as its name, a damaged
  feed file, the clean-up's own error, the two symbols' common days on SIP, and the request's params set
  in two places, each fixed (two offline tests in `tests/test_bars_to_catalog.py`); the unlocked file,
  a deleted file read as IEX, the quote write-back not reading the file and `catalog/sip/` inside
  `catalog/`, stated as limits in `canon_trading.bars_to_catalog` and here, since loads run one at a
  time under the paper lock and no quote load is built yet. Its third pass, of the lengthened test: the
  expected failure narrowed to the precision refusal; the comment on changing the plan and the
  docstring's account of when the sample was fixed; SIP's next-day booking across daylight time; the
  early-close check after the days', and one session-day computation a bar. Applied. Not applied: a
  longer timeout (the year loaded in 83 s) and IEX bars after 19:00 in winter (none in the year's run).
* Second reader (`evidence-verifier`), 2026-11-11, of the lengthened sample: the calendar, T = 250, the
  table, the request and storage arithmetic, Alpaca's limits, `chunk_size` and the Confirmation's
  figures hold; a trial's run time halved (one symbol a trial), the replay figures cited to ADR 0068, the
  sample held whole without streaming recomputed, the refused SIP bar's date no longer implied, the
  study id's month explained, and ADR 0076 and the sip feed's 2026-11-10 run marked as not in this
  repository. Applied above.
