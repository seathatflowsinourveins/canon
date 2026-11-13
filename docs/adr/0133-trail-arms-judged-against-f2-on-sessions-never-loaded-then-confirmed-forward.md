---
status: proposed
date: 2026-11-12
decision-makers: lead (the terms); landing lane (the means and the dates); repo owner ratifies
review-by: 2027-02-10
---

# The trail arms are judged against F2 on 42 sessions never loaded, then confirmed forward; the daily matrix's producer and the replay's date guard are not built

## Context and Problem Statement

The Mover's trailing stop has three registered configurations: F2, a fixed 2% trail, and OWN1 and OWN3,
the ATR trail at k = 1 and 3 with the cap arm `own`. The 60 sessions the gate uses (2026-08-18 to
2026-11-10) are spent for them: 42 are the development dates the replay has already ranked on, and the 18
held by ADR 0096 are for that record's early-entry arms alone (the lead, 2026-11-12). Paper windows on
account B now run these arms, which the lead reads under ADR 0074 as proof the mechanism works as
built, not as a ranking (2026-11-12), so the arms need a test of which is better, fixed before any result on its dates is read, and
a rule that undoes a default that the forward sessions do not bear out.

## Decision Drivers

* No outcome on a judged date is read before the dates, arms, measure, test and rule are on `main`.
* The comparison is the registry's own (arch's SPA and StepM through `arm_comparison`, ADR 0104), with
  the number of arms tried counted; the ranking module's differences from the Mover are stated.

## Considered Options

1. Judge on the 18 sessions ADR 0096 holds. Refused by the lead: they belong to that record's arms.
2. Judge entry by entry against F2, as the lead first worded it.
3. Judge per session day, each arm's day the sum of its entries' returns in planned-loss units, with F2
   as the benchmark, on sessions before the gate's 60 that no catalog here holds, then confirm forward.

## Decision Outcome

Chosen: **option 3.** It departs from the lead's wording "entry by entry" (option 2), and says so:
`arm_comparison` takes one row per session day (`arm_comparison.py:11-22`), arch's bootstraps resample
rows and take no grouping argument (ADR 0104, "Day blocks"), and OWN1 and OWN3 stop out and re-enter
at other bars than F2, so their entries do not pair one to one. The lead accepted the departure on
2026-11-12 for that reason.

* **Arms.** F2 (`trail_mode` fixed, `trail` 0.02), OWN1 and OWN3 (`trail_mode` atr, `atr_k` 1 and 3,
  `trail_cap` own), registered as the windows' `TRAIL_ARMS` (`session_window.py:530-535`), OWN1 the arm of a window
  that names none (`:540`), each window sized from the budget (`:1630`); the settings' vocabulary is
  `mover.py:525-533,565-584,936-938`. A tightening or ratchet
  arm joins only by an amendment registered before that arm's dates are read, once the replay can carry
  it. Nothing else is judged here.
* **The ranking module and how it differs from the Mover.** `canon_trading.reentry_replay`, with `fixed`,
  `own-1` and `own-3` as F2, OWN1 and OWN3 (`reentry_replay.py:83-95`). It is not the Mover:
  * bars only: no quotes, a walk over each minute's open, high, low and close (`:1-6`), where the Mover's
    stop is held on the bid by the local emulator;
  * the cap arms: `own-k` keeps its own width; the replay's `atr-k` arms are the clamp at 0.02 without
    the clamp's refusal of a spread floor over it, and there is no `refuse` arm (`:223-244`);
  * ATR warm-up: the replay's ATR runs over the day's bars from 04:00 (`:205`, `:373`). The Mover warms a
    slot's ATR when the slot opens: first from the cached minute bars of the session part open now
    (`mover.py:2679-2692,2913-2951`), then, if that leaves it short, by one request for the last hour or
    `atr_n` plus 5 minutes, which may reach into the part before (`:2699-2702,1364-1376`), at most 20
    requests a minute (`:490`). A slot whose ATR is not warm at entry (fewer than 14 bars, its request
    refused, held or not yet answered; 130 s only names a request late) enters on the cold fallback; once
    its ATR warms, its trail is re-selected once and the result taken only if tighter, and a top-tier
    plan keeps the cold one (`:496-498,2752-2837,4496-4545,4523`). So the two warm-ups differ in source and span; the 1-hour bound is
    UNCALIBRATED and left to this record (`:1369-1371`);
  * the cold fallback: a cold ATR always takes the fixed 0.02 plan (`reentry_replay.py:234-235`), where
    the Mover with `own` raises it to the spread floor when that is wider (`mover.py:4286-4322`);
  * no spread floor: it is explicitly not applied (`reentry_replay.py:14`); the Mover's is two quoted
    spreads (`mover.py:578-583`), and a zero ATR, which the Mover would floor (`:4326-4327`), is refused
    (`reentry_replay.py:569-570`);
  * re-entry: the replay's `reclaim` enters on any one close above the sale with no cooldown (`:299`,
    `:1098`). Paper windows default to the Mover's `clock` arm (`session_window.py:779`), which re-enters
    only after its 300 s cooldown, on a qualified run whose close is above the sale, with the name still
    ranked and a slot free (`mover.py:968,3745-3751`; the run is `_registered_run`, `:2347-2426`, in the pre-market and regular
    sessions, and the bridge path, `:2206-2212`, in the others). The Mover's own `reclaim` arm, with no
    cooldown but a qualifying close after the sale's fill (`:3753-3760`), is the replay's counterpart
    only in a window that names `--reentry-arm reclaim`;
  * slots and fills: every selected symbol-day is replayed on its own, with no slot limit, compounding
    or fees (`reentry_replay.py:29-30`); a stop sells at exactly its trigger, which leans in the
    strategy's favour (`:1109-1111`); the Mover holds `slots` names by live rank and falls back to the
    fixed plan with no usable spread (`mover.py:4229-4238`).
* **The cell judged.** Initial `run-3`, the run length paper windows use (ADR 0092's registered
  `run_bars`, `early_entry_study.py:185-189`; `session_window.py:2227-2231`; `mover.py:934`), not the
  replay's `run-2` base; re-entry `reclaim`; S1 sizing at the 20,000 slot budget; every tier pooled,
  over the population ADR 0092's rule lists on each date; both bar orders (`ohlc`, `adaptive`) and every
  half-spread (0, 5, 25, 100 bps): eight cells.
* **The measure.** Each entry's return in units of its planned loss; each arm's session day is the sum
  over its entries that day; a day with no entry on an arm counts as zero planned-loss units, an explicit
  0.0, not a missing day. Each entry's planned loss is its own: the slot budget's whole shares at the
  entry times the entry less its stepped 2% stop (`reentry_replay.py:103-104,424-433`), about 400 USD but
  not one constant. So a day's value is not the day's dollars over a constant, and this departs from
  `arm_comparison`'s "decimal portfolio returns on a common capital basis" (`arm_comparison.py:16-17`);
  the lead's measure is kept and the departure stated.
* **The comparison.** `arm_comparison.compare_arms` unchanged, benchmark F2, `block_size` 3 (the cube
  root of 42, rounded down; ADR 0104 leaves it to the caller and it is UNCALIBRATED), `reps` 10,000 and
  `seed` 20261113, this record's choices, since `compare_arms` requires both (`arm_comparison.py:226-229`;
  arch's own default `reps` is 1,000), and `size` 0.05, the default it already uses
  (`arm_comparison.py:288-310`).
* **The rule.** An arm is proposed as the default only if StepM puts it in the superior set in all eight
  cells; two such arms are ordered by their mean daily difference over F2 at `ohlc` and 25 bps, which the
  matrix's producer computes from the same matrix (the comparison's report does not give it). If none
  qualifies, F2 stays and the numbers go to the owner.
* **The historical set**, named now and never loaded on this machine: the 42 sessions 2026-04-21 to
  2026-06-18, with their 20-session lookback 2026-03-23 to 2026-04-20; list below. The gate's catalog
  already holds day bars from 2026-06-20 (its day-bar files begin `2026-06-20T04:00Z`, its minute-bar
  files `2026-07-21T08:00Z`), so the set ends before that.
* **The forward set**, confirmation only: the 20 sessions 2026-11-13 to 2026-12-14, early closes
  (2026-11-27, 2026-12-24) left out, listed below. Each is read once its `sip` minute bars are published
  and loaded by `paper:history` (ADR 0093), the same path as the historical set. A forward date counts
  only if this record is on `main` before that date's bars are loaded for it; a date loaded earlier is
  dropped and the set extended by the next regular session. The paper windows that run these arms on
  forward dates are ADR 0074's proof of the mechanism, not this record's measure, and are not read for
  its verdict. ADR 0100's later holdout (2026-11-12 to 2026-12-11) shares 19 of these 20 dates, and
  this set's lookback reads bars on its 2026-11-12; until the gate's fix, an opening of either must not
  mark the other's dates.
* **The rule that undoes a default**, whichever record or proof made it the default: after the forward
  set, the producer's mean daily difference of the default arm over F2 at `ohlc` and 25 bps, read alone
  with no test; if it is below zero, F2 is the default again. A default the lead sets on paper proof
  before this record's verdict is undone by the same rule.
* **A quote replay, not chosen here.** Whether NautilusTrader's backtest engine can replay catalogued
  quotes against the Mover's emulated trailing stop was read in the pin (`d53849b216e2`), and in part it
  can. The kernel the backtest engine embeds owns the order emulator
  (`crates/system/src/kernel.rs:121,424,468`; `crates/backtest/src/engine.rs:1200`). The emulator
  subscribes to quotes (`crates/execution/src/order_emulator/emulator.rs:169-190`) and recomputes a
  trailing stop's trigger on each one (`:1148-1167,1597-1637`; `crates/execution/src/trailing.rs:40`).
  The engine routes each quote to the simulated exchange and then to the data engine (`engine.rs:929-934`).
  A catalog data config accepts quote ticks (`crates/backtest/src/config.rs:859-863`), and the docs run
  the same emulator in every environment (`docs/concepts/orders/emulated.md`). The pin's test
  `test_emulated_trailing_stop_updates_trigger_and_releases`
  (`crates/backtest/tests/integration/backtest_engine.rs:5076`) updates, releases and fills an emulated
  trailing stop fed by `add_data`, but a buy `trailing_stop_market` on a crypto perpetual, with a trade
  tick among its data (`:5044-5058,5094`); the Mover holds a sell emulated `trailing_stop_limit` on an
  equity (`mover.py:3335,3346`), a path no test was found for (UNVERIFIED). So it can, in part, at that
  narrower scope. No test reads them from a catalog through `BacktestNode`,
  and the fill price of the released market order was not read: both UNVERIFIED. This record does not
  use it: no catalog here holds quotes for the historical set, which predates every catalog root (above),
  and a replay of the live `Mover` on recorded quotes is ADR 0101's open "quote and emulator parity". A
  quote arm joins only by an amendment, as a ratchet arm does. NautilusTrader v2.0.0rc9, after the pin,
  fixes emulated orders sharing a trigger instrument triggering from the wrong quote or trade source and
  changes `trailing_stop_calculate*` errors (its release notes); moving the pin re-reads this paragraph.
* **Open, judged by the replay** (added after the evidence-verifier's pass, on the lead's ruling of
  2026-11-13): whether a top-tier opening entry taken cold on the fixed plan should tighten once its ATR
  warms. A cold opening entry takes the fixed plan (`mover.py:4245-4258`); `_warm_tighten` re-selects a
  cold holding's trail once and keeps the cold plan when the re-selection is tier (`:4496-4545`, `:4523`).
  A calibration question.
* **Open, judged by the replay** (the lead's ruling of 2026-11-13, 03:25): whether a cross-dollar entry
  should be sized up to use the whole planned loss. An entry whose stop crosses a dollar, and whose
  rounded-down distance L already clears the floor, is sized at the price less its sub-dollar stop but
  protected at the shorter L on the entry's step, so it risks less than planned. Sizing up would reverse
  the rule the A3+O unit's test pins (`test_l_is_held_on_the_entrys_step_where_the_stop_crosses_a_dollar`,
  `tests/test_mover_sizing.py`, at `99781e0a`, not on `main` when this was written); the finding is a
  second reader's P2 on that unit. A calibration question.

### Consequences

* Good: the verdict is on sessions no process has ranked, by a published test that counts the arms.
* Bad: nothing can run yet. Three units are owed: the producer of the daily matrix from the replay's
  JSONL (none exists, `arm_comparison.py:22`), which must recompute each entry's planned loss, since a
  result carries entry returns, shares and per-share dollars but no planned loss (`reentry_replay.py:650-672`); the replay's date guard, which refuses every date from
  2026-10-16 (`reentry_replay.py:77,132-134`) and reads only `development` and `lookback` roles
  (`:137-153`), so it refuses the forward set and must refuse ADR 0096's 18 dates by their list; and
  the history load of the 62 historical sessions, after this record is on `main`.
* Bad: the forward set's 20-session lookback (2026-10-16 to 2026-11-12) reads bars on ADR 0096's held
  dates for baselines and ATR only, as the movers baseline already does (ADR 0096's freeze lists such
  reads); no outcome on them is read. The other choice, a forward set that begins after a lookback clear
  of them (about 2026-12-10), waits a month. The lead rules.

### Confirmation

The dates below are entered before any is loaded. A loader run on a historical date before this record
is on `main` breaks it. The verdict's report names this record, its commit and the eight cells.

Historical, judged (42): 2026-04-21 22 23 24 27 28 29 30, 05-01 04 05 06 07 08 11 12 13 14 15 18 19 20 21
22 26 27 28 29, 06-01 02 03 04 05 08 09 10 11 12 15 16 17 18. Lookback (20): 2026-03-23 to 2026-04-20,
Good Friday 2026-04-03 left out.

Forward (20): 2026-11-13 16 17 18 19 20 23 24 25 30, 12-01 02 03 04 07 08 09 10 11 14.

## Evidence

Read 2026-11-12.

* https://www.nyse.com/markets/hours-calendars — the 2026 holidays (Good Friday 04-03, Memorial Day
  05-25, Juneteenth 06-19, 07-03 observed, Thanksgiving 11-26) and early closes (11-27, 12-24); the
  dates above follow from it — fetched. The same derivation gives exactly the 62 dates of the development
  sessions file the replay ran on, a file outside the repository (UNVERIFIED for a reader of this record).
* ADR 0104, and `arm_comparison.py`, `reentry_replay.py`, `mover.py`, `session_window.py` and
  `early_entry_study.py`, read at `bf5742e0` and re-read at `efd37edb` (2026-11-13), where the lines are
  as cited. `efd37edb` has #340's ATR warm-up and #337's re-entry arms, which changed the warm-up and
  re-entry differences above.
* NautilusTrader at the pin `d53849b216e2`, read 2026-11-13 through `scripts/canon src`: the files cited
  under "A quote replay", read by a Sonnet subagent, with the test, the quote config and the emulator's
  quote subscription re-read by the landing lane.
* The gate's catalog file names (`sip-history-dev`: day bars from `2026-06-20T04-00`, minute bars from
  `2026-07-21T08-00`); every other root in the main checkout's catalog starts 2026-10-15 or later —
  listed, not read. Copies elsewhere UNVERIFIED.
* The evidence-verifier's pass on the draft, 2026-11-12: 30 claims, 21 held, 7 half, 1 failed (the
  measure called dollars over a constant). Its twelve corrections were reproduced and made above; the
  largest moved the judged cell from `run-2` to `run-3` and added four differences from the Mover.
* The evidence-verifier's pass on this record at `efd37edb`, 2026-11-13: about 40 claims, 9 half, none
  failed, and two overlaps not named. Its corrections were reproduced and made above: five line cites,
  the cold entry and the re-selection, the run's two paths, `reps` and `seed` as this record's choices,
  the pinned test's narrower scope, rc9's emulator fix, ADR 0100's shared dates, and a forward date's
  standing while this record is not on `main`.
