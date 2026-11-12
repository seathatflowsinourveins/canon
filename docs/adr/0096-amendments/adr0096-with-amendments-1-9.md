---
status: proposed
date: 2026-11-11
decision-makers: the owner for the 30-minute horizon (2026-11-11); the lead, under the owner's delegation (ADR 0074), for the split by data source and the holdout's one opening (2026-11-11); the lead, on the owner's word (2026-11-11), for the ignition amendment
review-by: 2026-12-11
---

# Early-entry study, fast arms: one-to-five-minute listing and entry arms registered on three data sources before any result, judged with ADR 0092's fifteen at one holdout opening for every arm ready when it opens

## Context and Problem Statement

The owner's requirement is that a move's edge is gone within 30 minutes. That horizon is a requirement, not a
measured edge. ADR 0092's fifteen arms list a name on a since-04:00 or 15-minute dollar-volume ratio and enter
on three rising closes, so naming a mover can take many minutes. This record registers faster arms, on 1, 3
and 5-minute windows, before any of their results is read. It extends ADR 0092, changes none of its rows, and
departs from it in two places, named under the holdout below. Every threshold here is UNCALIBRATED, and no arm
is implemented or run by this record.

## Decision Drivers

* Register before any result is read, and open ADR 0092's holdout once.
* No arm that can run on data that exists waits for data that does not.
* One idea measured on two data sources stays two sets of rows, never mixed.

## Considered Options

1. Amend ADR 0092 in place with 30 arms and one 45-arm selection that needs every input. Its minute sums
   come only from the recorder, which started on 2026-11-11, so no selection can run on the study's
   population. Meanwhile the fifteen could open the holdout first and leave the new arms none.
2. A record of its own, split by data source, with one holdout opening for every arm ready when it opens.

## Decision Outcome

Chosen: **option 2**, the lead's decision of 2026-11-11.

**Four data forms.**

* **Bars.** ADR 0092's fifteen arms take their earlier sessions from `paper:history`'s `sip` minute bars
  (ADR 0093). This record adds nothing they need; they run once ADR 0092's population is loaded.
* **Bar-derived rolling.** For `H` = 1, 3 and 5 at each completed New York minute end `T`, sum the minute
  bars that open in `[T-H, T)`. Shares are the bar's volume, dollars are its volume times its `vwap`, and
  its `trade_count` is kept alongside, never used as a threshold. Alpaca's `vwap` counts only prints
  eligible for both high/low and volume, so odd lots (`I`) and `R` add to volume but not to `vwap`: the
  dollars differ from the recorder's by construction. A null `vwap` or `trade_count` makes that minute
  unknown. `RVb_H` and `RDb_H` divide each sum by
  its mean over the same clock minutes in the latest 5 earlier sessions. A bar Alpaca did not send counts
  as zero only in a window the load completed without a stop. **Not ready:** the catalog's bars keep no
  `vwap` or `trade_count` (`parse_bar` writes OHLCV), so this form waits for `history_load` to keep both
  beside each bar, a Codex build the coordinator started on 2026-11-11, not on `main`.
* **Recorder sums.** The same ratios, `RV_H^m` and `RD_H^m`, come from the external recorder's sums per
  symbol, per minute and per condition set. They take the volume mask below and need all 5 baseline
  intervals covered, with no fallback. The recorder started on 2026-11-11 (the lead's word, UNVERIFIED
  here), so the earliest ready session is the sixth with five recorded sessions before it: 2026-11-18 at the
  earliest (calendar UNVERIFIED), later for any clock minute a gap leaves uncovered.
* **Historical prints.** The print arms wait for the historical-prints loader (ADR 0093's clause 9),
  which must keep each print's exchange, tape, raw price, conditions and correction flag `u` beside its
  `TradeTick`; alpaca-py's historical `Trade` model drops `u`.

Bar-derived and recorder-sum rows measure one idea on two data sources. Alpaca's bars count prints by
its own aggregation rules, and the recorder's sums count them by this record's mask. Any disagreement
between the two is reported as a finding, never averaged away.

**The holdout opens once**, after this record lands, at the first of two moments: when the bar-derived rows'
inputs cover the training dates (the `history_load` extension's `vwap` and `trade_count` in the catalog), or
09:30 New York on 2026-11-12. The lead's reason: the fast rows are the ones the owner's requirement is about,
and nothing on the order path needs the holdout tonight; the walk-forward training folds and the tuning study
run meanwhile. It opens for every arm whose inputs cover the training dates at that moment: the fifteen; the
bar-derived rows, once `history_load` keeps `vwap` and `trade_count`; `L_H-S` and `L_H-N`, if the prints
loader has landed; and the opening-range arms `orb5-rank-all` and `orb5-rank-bullish`, if their registration
has landed. ADR 0100's `F-all` and seven bar factor arms join it too if that record is on `main` before it
opens, as registered arms whose replay may follow the opening on the same dates, with nothing about them
changed after any result is read, reported outside this record's selection and tie order (the lead,
2026-11-11); otherwise they take ADR 0100's own later holdout. ADR 0100's `rdb5` and `vwap` arms take that
later holdout in either case. The recorder-sum arms and the arms that list through `C0` or `C15` cannot cover
sessions before 2026-11-11, so they are never ready at the opening. An arm whose data does not exist when the
holdout opens, or that is registered after it opens, is judged later, on a holdout drawn only from sessions
recorded after 2026-11-11, with its dates fixed before they are read. The walk-forward training folds may run
once the population is loaded; only the holdout waits. At the opening, for every arm in this record's
selection, ADR 0092's method, costs, fill model and DIAGNOSTIC claim apply unchanged; ADR 0100's arms take
that record's. Ties break in this order: the fifteen, `BV1`, `BD1`, `BV3`, `BD3`, `BV5`, `BD5`, `BEV1`,
`BED1`, `BEV3`, `BED3`, `BEV5`, `BED5`, then the two opening-range arms, then `L1-S`, `L1-N`, `L3-S`, `L3-N`,
`L5-S`, `L5-N`. A fold with any ready arm's training input missing reports **no selection; input-incomplete**,
names the input, and never falls back to a subset.

Departures from ADR 0092, the lead's decisions: its one holdout replays the winner among up to 35 arms, not
15; and the later holdout is a second one, on sessions after 2026-11-11 rather than ADR 0092's population,
where ADR 0092 refuses a second opening. The second is drawn for arms that could not be ready at the first and
never reopens the first. A third, the lead's decision of the same evening: ADR 0100's arms may be replayed on
the first holdout's dates after its other results are read, outside the selection, with nothing about them
changed.

**Arms.** Every listing also requires the unchanged daily test: `C(T) / reference_close - 1 >= 3%`,
where `C(T)` is the close of the latest completed bar since 04:00 at or before `T`, carried through
quiet minutes and unknown before the first bar. `S` is three strictly rising adjacent closes (four
closes), as `Mover.on_bar` counts them. `N` allows ties. Both start from listing and reset when listing
is lost; a minute without a bar supplies no close.

| Form | Listing arms (entry `S`) | Entry arms | Paired with |
|---|---|---|---|
| Bar-derived | `BV_H`: `RVb_H >= 3`; `BD_H`: `RDb_H >= 3` | `BEV_H`, `BED_H`: under ADR 0092's registered listing, the first minute end with that ratio `>= 3` | `registered` (ADR 0092) |
| Recorder sums | `LV_H`, `LD_H` | `EV_H`, `ED_H`: the same, under `C0` | `C0-S`, `C15-S`; `C0-S`, `C0-N` |
| Recorder sums, controls | `C0-S`, `C15-S`, `C0-N`, `C15-N` | | the original bar ratios, reported apart |
| Prints | `L_H-S`, `L_H-N`: `M_H >= 3%`, `R_H >= 3` and `P(t) / reference_close - 1 >= 3%` | `C0-E_H`, `C15-E_H`: the first eligible print with `M_H >= 3%` and `R_H >= 3`; `C0-F`, `C15-F`: the first print above `B` | `C0/C15-S/N` |

`C0` is the since-04:00 dollar-volume ratio and `C15` the latest completed 15-minute segment's ratio, each `>=
3`. Both are computed from the recorder's volume-eligible sums at minute ends, over the recorder's registered
list (the lead's word, UNVERIFIED here), so the recorder gap rule below holds for every arm that lists through
them, print arms included: `C0-E_H`, `C15-E_H`, `C0-F` and `C15-F` need both prints and recorder sums. `C0-S`
differs from ADR 0092's `registered` arm in two ways. It uses trade dollars where `registered` uses close
times volume. It also requires all 5 earlier sessions covered, where `_listing` averages whatever earlier
sessions exist and lets a minute with no trades add zero.

Print measures, for each price-eligible print at event time `t` on the tape prefix through it: `P(t-H)` is
the latest price-eligible print at or before `t-H` that day, unknown without one. `M_H = P(t) / P(t-H) - 1`.
`D_H` is the sum of price times size of the unique volume-eligible prints in `(t-H, t]`. `R_H` divides it
by the mean over the same clock interval in the 5 earlier sessions, all covered. `F` freezes `B` from the
last eligible print before the listing minute end `T`, and fires only on a print at or after `T`.

**What counts.** Print eligibility follows Alpaca's condition table by tape: price-eligible means green for
the minute bar's open and close, and volume-eligible means green for volume. An absent, malformed or unmapped
code, or an unknown tape, is unknown. The recorder's volume mask includes `" "`, `@`, `A`-`H`, `I`, `K`, `L`,
`N`, `O`, `P`, `R`, `T`-`Z`, `4`-`7`, and excludes a bucket holding `M`, `Q` or `9`; a bucket holding any
other code is unknown. The mask is tape-free because a bucket carries no tape. The table defines `" "` and `@`
but not an empty list `[]`. Treating `[]` as a regular sale on tapes A and B in the prints, and as eligible in
the recorder's mask, is this record's choice. `[]` on tape C is unknown in the prints. A historical row whose
`u` is `canceled` or `incorrect` is dropped; one whose `u` is `corrected` is kept once, at its own time; the
loader must read `u`. Any known correction or cancel in recorder data voids that symbol-day for the recorder
rows.

**Timing and fills.** A minute-end signal fills at the first bar with `ts_init >= T`, and a print signal at
the first bar with `ts_init > t`, both by ADR 0092's close plus half spread. A print at 09:31:00 fills at
the 09:32 close at the earliest. A historical timestamp is the tape's time, never this machine's receipt
time; only a live window's event-to-submit time measures the reaction.

**Recorder gaps.** A gap inside `[04:00, 16:00)` leaves that whole symbol-day unscored for every arm that
reads recorder sums. It is never interpolated, and a gap in a baseline interval makes that interval
unavailable, never zero. On 2026-11-11 the lead reported gaps at `[14:00, 14:01)`, `[14:40, 15:50)` and
`[17:33, 18:29)` New York; the first two void that day. Gaps do not touch the bars, or `L_H-S` and `L_H-N`,
which list on prints alone.

### Consequences

* Good: the fifteen and the bar-derived rows can be judged without waiting on the recorder or the prints.
* Good: two data sources for one idea give a check on each other.
* Bad: the recorder-sum and print arms get a later holdout of fewer sessions than the fifteen get.
* Bad: up to 20 added rows join the opening's selection, so the loosest of more arms can win a fold.

### Confirmation

* Before the holdout opens, the run names the arms that are ready and the commit of each one's registration.
* The later holdout's session dates are entered here before any of them is read.
* `proposed` until `history_load` keeps `vwap` and `trade_count`, and the prints loader and the recorder's
  baseline sessions exist.

## Evidence

Read 2026-11-11. The thresholds, masks, names and pairings are this record's choices, not an upstream's.

* `packages/canon-alpaca/src/canon_alpaca/data.py` at `main` — `parse_bar` builds a NautilusTrader `Bar`
  from open, high, low, close and volume only. Read.
* alpaca-py at `c42add7` — `alpaca/data/models/bars.py`: `Bar.vwap` and `Bar.trade_count`, both
  `Optional`; `alpaca/data/models/trades.py`: the historical `Trade` keeps no `u` field
  (`TradeCorrection` and `TradeCancel` are stream models). Resolved.
* https://docs.alpaca.markets/us/reference/stocktrades-1 — historical trades carry `u`: `canceled`,
  `incorrect` or `corrected`. Fetched by the evidence-verifier on this record, 2026-11-11.
* https://docs.alpaca.markets/us/docs/market-data-faq#how-are-bars-aggregated — the condition table by tape
  (price and volume columns), with `" "` and `@` defined and `[]` not; `vwap` counts only prints green for
  both high/low and volume. Fetched by the evidence-verifier on
  the draft, 2026-11-11.
* `src/canon_trading/early_entry_study.py` at `main` — `ARMS` (the fifteen) and `_listing` (the baseline
  averaged over the sessions that exist). `src/canon_trading/mover.py` — `Mover.on_bar`'s four closes. Read.
* ADR 0093 at `main` — `paper:history` loads `sip` minute bars; its clause 9 refuses trades until the
  adapter's `_request_trades` exists. Read.
* The lead's decisions on the split, the holdout and `C0`/`C15`'s source, relayed by the coordinator at
  20:40 New York; the recorder's contract and its gap times from the lead at 19:40 and 19:45. The
  recorder's code and coverage were not inspected: UNVERIFIED.
* Draft: a Codex builder's ADR 0092 amendment (`7bcbd60`), with a DeepSeek Harness round and two
  Antigravity rounds folded in. The evidence-verifier on that draft found its three method gaps, which
  the lead's decisions above answer.
* evidence-verifier on this record, 2026-11-11: the claims held but two; it found the `u` field's
  source, the `vwap` subset, the holdout rule's reading, the gap rule's reach into the print arms, the
  mask's other codes and two unstated departures from ADR 0092. Corrected above.

## Amendment, 2026-11-11: ignition and its paired tape

Decided by **the lead, on the owner's word (2026-11-11)**, before any result of this tape is read:
the row ignites at ADR 0092's measure **0**, admits entry within **5 minutes**, inclusive
(`EARLY_WINDOW_MINUTES`, this record's longest fast horizon), and orders early names by **`RDb_1`**,
then by the existing score. A missing one-minute normal gives an unknown ratio, ordered after known
ratios. Its paired arm, `entry_age_rule = "any"`, enters the top score at any age.

The pair is judged walk-forward on **ADR 0092's development window**, once that history is loaded;
the replay takes a catalog root and a session list. A replayed session's baseline uses only
its latest **5 earlier sessions**; the first five loaded sessions are lookback, never judged. ADR
0092's authoritative method on `main` (`1e790c7`, 2026-11-11) reserves the last 18 of the 60 regular
sessions before 2026-11-11 as the holdout. The loaded holdout sessions, excluded from judging, are
**2026-10-16, 2026-10-19, 2026-10-20, 2026-10-21, 2026-10-22, 2026-10-23, 2026-10-26,
2026-10-27, 2026-10-28, 2026-10-29, 2026-10-30, 2026-11-02, 2026-11-03, 2026-11-04,
2026-11-05, 2026-11-06, 2026-11-09 and 2026-11-10**. These dates are the final 18 earlier
sessions actually present in the catalog's bar-extras sidecar (New York dates, not UTC dates).
The loaded 20-session `sip` history in `catalog/sip-history-20`, 2026-10-15 through 2026-11-11,
therefore supplies no development session with five earlier sessions. **2026-11-11** alone is a
**mechanics replay**, outside that population window: it shows the rule runs and what it would have
entered, is no judgment, and shifts no setting. Paper windows traded on the night of 2026-11-11.
The lead confirmed before any result existed that holdout sessions may be read **only as the
five-session baseline for a later replayed day**: a baseline is an input to the normal, not an
outcome. The tape refuses to emit any per-arm result for a holdout date, covered by a synthetic test.
The listed dates were derived by taking the final 18 distinct New York session dates earlier than
2026-11-11 in SPY's `bar_extras` timestamps, accounting for the November clock change; UTC dates
spill into the next day and are not sessions. **`RDb_1` uses the exact per-minute normal**; a
15-minute segment divided by 15 is never a substitute on this tape.

Report ADR 0092's measure **0** (ignition), **1** (entry lead), **2** (share still ahead after the
entry bar and share done), and **3** (never entered, late, and missing, with their denominators),
plus `move_ahead_at_entry = (high - entry) / (high - reference)` over ADR 0092's 04:00–16:00 window,
null when `high <= reference`. Measure **4** needs the registered quote costs and managed execution
inputs, so is unavailable on this bar-only tape, explicitly UNVERIFIED, with no substitute outcome.
No other measure is introduced, and no threshold is tuned on this tape. Every number is
**UNCALIBRATED** and every result **DIAGNOSTIC**: the pair ranks nothing and records no ledger trial.

**Clarifications before any result, 2026-11-11.** The bar-derived form's readiness above has changed:
the loaded roots now carry the `bar_extras` sidecar with `vwap` and `trade_count`, written by
`history_load` on branch `land-history-ext`, not yet on `main` (the coordinator's source attribution;
the loaded sidecar's schema read here). This tape's `RDb_1` depends on it and uses volume times
Alpaca's `vwap`. The early-name ordering by that ratio, then score, with an unknown ratio last, is a
**new choice by the lead, on the owner's word (2026-11-11)**, not a deduction from either record.
Ignition keeps ADR 0092 measure **0**'s cumulative volume condition from the part's start, under the
ranking's existing numerator span; neither the fast ratio nor this record's daily test defines it.

This pair is **not one of the arms ready at this record's single holdout opening**, is never replayed
on that holdout, and is judged on the development window only. Any holdout use for it needs a later
amendment committed before that opening. **No loaded session is judgeable today**: the pair waits for
the development load in `catalog/sip-history-dev`; November 11 is mechanics only. The first pass
allocates seven slots without rotation, using the snapshot Mover's entry decision and budget/flow
sizing, with simulated cash and buying power both **200,000 USD**, identical for both arms and
**UNCALIBRATED** (the coordinator's reversible decisions, 2026-11-11). Measures **1** and **2** are
reported for that pass; measure **4** and refilling slots after managed exits are subsequent work,
with quote costs and outcomes **UNVERIFIED** until their inputs exist.

**Further amendment before any result, 2026-11-11, the lead's decision.** The age cap is a setting
to measure: `entry_age_max_minutes` in **{1, 3, 5, none}**, all **UNCALIBRATED**, with `none` restoring
tonight's score ordering and entry at any age. Cross these with ADR 0092's rising-run row
(`run_bars` **{1, 2, 3}**, its arms table) and this record's entry arm **`BED_H`** (its Arms table:
the first minute end under measure-0 listing with `RDb_H >= 3`, **H = 1, 3, 5**). These are existing
arms, not new entry definitions. With three rising closes the earliest entry is three minute-ends
into the run, so cap 1 enters almost nothing under entry `S`. This extends the first-pass pair
above; every setting remains UNCALIBRATED and nothing shifts on November 11.

Register a **lead curve**, not one verdict. For every ADR 0092 measure-**0** ignition on judged
development days, its table has rows at leads **0, 1, 2, 3, 5, 10, 15 and 30 minutes**, split by
session part and by dollar-volume tier. Each row gives its count, measure **2**'s share of the move
still ahead, measure **3**'s late share, and measure **4**'s outcome at half-spread and twice it.
The split uses ADR 0087's established and unestablished dollar-volume tiers at ignition; unknown
establishment is reported separately. Missing lead-minute bars and ignitions before the seed's
reach are counted separately, never filled from future bars. No threshold is tuned on the curve.
The November 11 curve is **"one day, not a judgment"**, mechanics only, on `sip` from **04:00–20:00**,
with its five earlier sessions read as baseline only. The overnight part is included only if
`catalog/boats-history-20` supplies its baseline through existing code; otherwise it is absent.
Ignition is always the builder's `ignition_from_bars`, never a second measure-0 implementation;
the per-minute normal and `rdb_1` use the exact sidecar dollars already required above.
Quote costs stay **UNVERIFIED**. Without quotes the curve gives the gross zero-cost outcome,
explicitly labelled zero cost, and leaves the half-spread and twice-half-spread outcomes unknown.
The development curve waits for its separate load; the holdout prohibition above still applies.

**Sub-minute tape registered before any result, 2026-11-11, the lead's decision.** A second tape
uses prints. Bars find each ADR 0092 measure-**0** ignition minute through the builder's function.
For that symbol only, load prints from **5 minutes before to 30 minutes after** through
`paper:history --data trades`, into a separate catalog root: the ignition list bounds the load,
never the whole market. Onset is the first print where the running dollar volume of the clock
minute in progress reaches **3 times that minute's per-minute normal**, with the listing move test
on that print's price. This uses the same registered ratio and minute baseline, no new threshold.
The entry leads are **0, 5, 10, 15, 30 and 45 seconds** after onset, filled at the first print at or
after onset plus lead; the minute-end entry is their paired row.

Report the same curve outputs, ADR 0092 measures **2, 3 and 4**, split by session part and dollar
tier. Costs are ADR 0092's half-spread and twice it, **assumed** until historical quotes exist, with
the zero-cost limit above explicit when no quote cost is supplied. All numbers are **UNCALIBRATED**;
November 11 comes first as **mechanics only**, and the holdout rule is unchanged. Condition codes
are not applied: the catalog's `TradeTick` drops conditions, exchange and tape (ADR 0093 clause 9;
`history_load.py`'s historical-prints contract), so this tape cannot implement the condition masks
of the prints arms above and must report that limit. Live sub-minute detection is a **separate
build behind a setting, off by default**, outside this tape.

### Evidence for the time scale, 2026-11-11

Context only; **no threshold is taken from either source**. The lead's scout read both in full and
recorded these limits in `reviews/scout-ignition-horizon.md` outside the repository; this session
checked the first paper's abstract and the second paper's supplied full-text source.

* Zawadowski, Kertész and Andor, [Large price changes on small scales](https://arxiv.org/abs/cond-mat/0401055)
  (2004), arXiv cond-mat/0401055: price, volume and spread after extreme 15-minute intraday moves
  on NYSE and NASDAQ; overreaction and decaying volatility. The scout's full-text reading concerns
  liquid stocks above $10, not microcaps or an extended-hours burst study.
* Zarattini, Barbon and Aziz, [A Profitable Day Trading Strategy for the U.S. Equity Market](https://alexandria.unisg.ch/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/download)
  (2024), SSRN 4729284: opening-range windows of 5, 15, 30 and 60 minutes on US stocks, anchored
  to the regular open. The scout's full-text reading reports commissions but no spread/slippage
  model; this is context for comparing windows, not evidence of when a volume burst stalls.

## Amendment 4: two dollar sources, registered 2026-11-12 before any ordering comparison

The lead's decision of the night of 2026-11-11: **`RDc_1`** is the latest minute's **close times
volume**, over the normal for that same clock minute in the five earlier sessions, computed the
same way. This is ADR 0092's registered dollar form: `movers_stream.minute_dollars` multiplies
close by volume at `src/canon_trading/movers_stream.py:333-338`, and `normal` averages the same
minutes over earlier sessions at `:342-348`. The per-minute baseline carries **both** close-times-
volume normals and volume-times-Alpaca-VWAP normals where VWAP exists; neither is a proxy for
the other. **No typical-price proxy is permitted.**

Live, at **04:00**, early names are ordered by **`RDc_1`**, named `RDc_1` in rows and records,
never called `RDb_1`. Early names **do not fall back to the score**. The tape keeps **`RDb_1`**
(exact Alpaca VWAP dollars) and **`RDc_1`** (close dollars) as **two sets of rows**, following this
record's rule for two sources of one idea. On the same completed-minute generations, universe,
age cap and filters, compare their published top-20 orders and report the number of comparable
generations and the number whose orders differ; unknown inputs are named, never replaced by
typical price or by the other dollar form. No threshold is tuned from that comparison.

The earlier November 11 lead curve has no ordering comparison and is **SUPERSEDED: pre-fix
ignition (L4 findings 2–4); not for reading as coverage**. Its files carry the `-prefix` suffix.
The fixed builder's ignition is required before replaying the original filenames: its fixes must
address censoring on a name's first bar, losing the first qualifying minute at 04:00, and zero-mean
names never igniting. No `RDb_1`-versus-`RDc_1` result existed when this amendment was registered.
Carrying VWAP through the adapter is a later build, started only if the tape shows materially
different orders, a decision for the lead; **not tonight**. All numbers remain UNCALIBRATED and
results DIAGNOSTIC, and the holdout prohibition is unchanged.

## Amendment 5: kind is a stratum, 2026-11-12

The lead, on the owner's decision, replaces the earlier fund-exclusion instruction **before any
fund-related result**: funds, leveraged and inverse funds included, **stay candidates**. Kind is
an **UNCALIBRATED name-proxy label** (`share`, `fund`, `warrant`, `unit`, `right`), with shares
and funds reported apart on the tape. It is **a stratum, not an arm**; no excluded/admitted fund
setting is registered, and being a fund excludes nothing. The proxy names a fund on ETF, ETN,
Fund or Trust, or on a leverage marker (2X, 3X, 1.5X, Bull, Bear, Inverse, Short, Ultra) together
with Daily or Shares. The existing warrant/unit/right filter remains. An official security-type
source is later, low priority, through `adopt-upstream`; the label must not delay the ignition fix.

## Amendment 6: the approved plan's entry clock and tier, 2026-11-12

The lead, under the owner's approved consolidated plan, Part A, replaces the earlier operational
orders before any result of these rows is read. **Age starts at the close of the ignition minute**,
the minute when the condition is known. The rising run **may count completed seeded closes**;
no close later than the entry's clock counts. The **04:00 arm is cap 5 / `run_bars = 2`**, with
**`run_bars = 1`** its named alternative. These are the already registered cap and run settings,
all **UNCALIBRATED**; the new starting choice is not a result of the superseded tape.

Each ignition carries **`ignition_basis`**. For a name **without an established baseline**, basis
**`tier`** is the first minute meeting the registered move condition whose part-to-date dollars
reach **the tier's admission bar**, under ADR 0087's existing tier rule. This is a **named
departure from ADR 0092 measure 0**, **UNCALIBRATED**, reported apart from basis **`ratio`**.
The builder's public ignition functions own both bases; this tape implements no second ignition
rule. The dollar form remains close times volume, with exact VWAP dollars a separate source.

**Unknown `RDc_1` does not block entry.** Known ratios order first by value; unknown ratios follow
by **latest-minute dollar volume**, with every tie broken by **instrument ID**, never score.
This replaces the earlier unknown-means-no-entry instruction; unknown ratios remain named and
counted, never presented as known, and close dollars are never called `RDb_1`.

Part A's named base is **C0**, the committed firing composition, whose SHA the landing lane will
supply. Every builder patch rebases on C0; a paper firing runs only from a committed SHA. Before
a landing lane commits builder work it checks `git merge-base` against C0 or `main` and names
which it checked. This worker remains uncommitted. The fixed builder patch and **dev root
complete** are still required before replay; the holdout and DIAGNOSTIC limits above remain.

## Amendment 7: re-entry and opening arms, 2026-11-12

The lead, on the owner's correction, registers these rows before any result. Their source is
**lead, no primary source**; every number is **UNCALIBRATED**. **No re-entry arm has a time
cooldown.**

* **`reclaim`**, the paired row: a close back above the sale price, cooldown **0**.
* **`turn`**: the first run of **k rising closes** from the lowest close since the sale, with
  **k = 1, 2, 3**, base **2**, if that low is at least **d trail-distances** under the sale,
  **d = 1, 2**, base **1**, and the name is still ranked and eligible.
* **`vwap`**: the first close back above the session VWAP after a close below it. This is
  **tape only** until the adapter's `vw` lands; no typical-price proxy supplies it.

**The opening arm**, regular session only: among **top-liquidity-tier ranked names**, in the
first **M minutes after 09:30**, **M = 15, 30**, base **30**, enter on the turn from the opening
low when the fall from the open is at least **f trail-distances**, **f = 1, 2**. Its lead is
**the owner's observation**, not measured evidence from a primary source.

Per-symbol tables are **DIAGNOSTIC**, never fitted per-symbol settings. Settings are chosen
**per tier**, with per-name scaling through **ATR**. A separate builder's per-symbol replay
runs on **development days only, never a holdout date**. These rows change no holdout opening
and select no setting from the superseded November 11 tape.

## Amendment 8: R3's fixed replay choices, 2026-11-12

Before R3's first result is read, the lead fixes these choices, all **UNCALIBRATED**. The exit
rows follow T1's registration, `cx-t1-t2-trail/REPORT.md:13-21` (read 2026-11-12): **`atr_k = 2`**,
alternates **1, 3**; **`atr_n = 14`** completed minute bars, alternates **7, 28**; baseline-dollar-
volume tier cut **1,000,000 USD per minute**, alternates **500,000 and 2,000,000**, a stand-in for
size rather than market capitalization. The tier trail is **0.002**, the owner's registered tier
row. On bars **without quotes, the spread floor is not applied**, and every result names that limit.

**No registered half-spread exists for the development window.** R3 reports a sensitivity grid
at **h = 0, 5, 25 and 100 bps per side**, with each arm's **breakeven half-spread**. These costs
are assumptions, **UNCALIBRATED**, not measured spreads and not a substitute claim about quote costs.

For this R3 replay, an ignited name's first entry is the run **after ignition**, **`run_bars = 2`**,
with **1 and 3** also reported. A **top-tier name that never ignites** gets a scaffold entry of
**2 rising closes after 04:00**, labelled **"scaffold entry, not an arm; lead: none"**. This scaffold
does not invent an ignition or an entry-lead measurement. **Per-symbol tables are diagnostics only**;
the development-only and no-holdout rules of amendment 7 remain.

## Amendment 9: one flow-cap alternate, 2026-11-12

Before any result of the alternate is read, the lead registers **`flow_share = 0.01`** beside
the existing default **0.05**, both **UNCALIBRATED**. Canon keeps its existing flow window and
budget/sizing rules. The source is Lean's VWAP execution model: it defaults its order cap to
**1% of the current bar's share volume** and applies that cap through its order-sizing helper.
Read 2026-11-12 in the primary
[VolumeWeightedAveragePriceExecutionModel source](https://github.com/QuantConnect/Lean/blob/master/Algorithm.Framework/Execution/VolumeWeightedAveragePriceExecutionModel.py),
`maximum_order_quantity_percent_volume = 0.01` and `get_order_size_for_percent_volume`.
This is a dated source for the alternate, not an imported execution model.

**Note only, not an arm:** backtesting.py's
[TrailingStrategy source](https://github.com/kernc/backtesting.py/blob/master/backtesting/lib.py)
defaults `set_trailing_sl(n_atr=6)` to six ATRs (read 2026-11-12). This adds no six-ATR arm and
changes none of amendment 8's trail rows. All replay results remain DIAGNOSTIC and the
development-only and no-holdout rules remain.
