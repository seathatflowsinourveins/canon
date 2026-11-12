---
status: proposed
date: 2026-11-11
decision-makers: the lead, under the owner's delegation (ADR 0074), for the factor study's order, its registration before any result, and which opening its arms join (2026-11-11); master orchestrator (Claude) for the factors, arms and result rule below
review-by: 2026-12-11
---

# Early-entry study, factor arms: seven bar factors, a price-tier stratum and two waiting on vwap, measured at each part's first listing minute and registered before any result; `F-all` and the seven bar factor arms join ADR 0096's first holdout opening if this record is on main before it opens, else all ten arms go to this record's later holdout

## Context and Problem Statement

ADR 0092 names a mover's first listing minute and ADR 0096 adds faster listing arms. Neither asks which
facts known at that minute separate the listings worth entering from the rest. A reader's inventory of 96
candidate factors (F1, 2026-11-11) found most need data this repository does not keep: quotes, print
conditions, float, short interest, options, news or filings. A Codex builder's population module (F2,
`mover_population.py`) derives each symbol's first listing in each session part. This record registers the
factors that bars can supply, their arms, and what counts as a result, before any is computed. Every
threshold and cut here is UNCALIBRATED, and no arm is run by this record.

## Decision Drivers

* Register before any result; ADR 0092's holdout dates are read only for ADR 0096's one opening, the later
  replay of this record's arms included, and never for tuning.
* A factor enters only with a definition fixed here and a named source, or "lead, no primary source".
* The owner's package (its fitted weights and its top-100 list) goes into no arm; its days stay labels.

## Considered Options

1. Wait for prints and quotes, then register every factor F1 found definable.
2. Register now the factors the catalog's bars supply; leave the rest to later records.

## Decision Outcome

Chosen: **option 2**. It needs ADR 0092's fixed window (PR #274) on `main` first.

**Population.** F2's derivation over ADR 0092's 60 regular sessions before 2026-11-11 (its `--start`
and `--end`): by ADR 0092's listing condition (`_listing`), with F2's added rule that all 5 earlier
sessions be covered. It is taken separately in pre-market `[04:00, 09:30)`, regular `[09:30, 16:00)` and
after hours `[16:00, 20:00)`, New York, with dollars since 04:00 in each. The event is a part's first
qualifying bar close, minute `T`, so a symbol has up to three a day. "Every symbol" means every symbol in
the catalog: the asset-list load is not built. F2's overnight events and gaps are discarded; whether `sip`
history serves overnight is UNVERIFIED. Each event keeps F2's quiet control: a different symbol at the
same session, part and minute, every close of that part within ±3% of its own reference (after `T`
included), nearest on reference and baseline, with replacement. An event without one is kept and named.

**Factors**, from bars with `ts_init <= T`. Unknown where an input is missing.

| Id | Definition | Source |
|---|---|---|
| `move` | `C(T) / reference_close - 1` (ADR 0092's reference) | ADR 0092's registered row; lead, no primary source |
| `rdv` | dollars (close × volume) since 04:00 over the mean of the same minutes in 5 earlier sessions | ADR 0092's registered row; lead, no primary source |
| `gap` | first regular bar's open / reference close − 1; unknown for a pre-market event | lead, no primary source |
| `pmadv` | shares in `[04:00, min(T, 09:30))` over the mean volume of the 20 earlier daily bars; unknown with fewer | lead (owner's package), no primary source |
| `newhigh` | 1 if `C(T)` exceeds every bar high since 04:00 before `T`'s bar, else 0; unknown with no earlier bar | lead, no primary source |
| `streak` | strictly rising adjacent closes since 04:00 ending at `T`, capped at 3; a minute with no bar adds no close. Unlike ADR 0096's `S`, it does not start at listing | lead, no primary source |
| `repeat` | 1 if the symbol has an event in this population in the 20 earlier sessions, else 0; unknown in the window's first 20 | lead, no primary source |
| `tier` | reference close below 1, 1 to 5, 5 to 20, 20 or above, in dollars; a stratum, not an arm | lead; boundaries this record's choice |
| `rdb5` | ADR 0096's `RDb_5` at `T`; unknown if its baseline is zero or any `vwap` or `trade_count` in its windows is missing | ADR 0096; lead, no primary source |
| `vwap` | 1 if `C(T)` is above sum(vwap × volume) / sum(volume) since 04:00, else 0; waits for `vwap`. Leaves out odd lots and `R` (F1 finding 4) | lead, no primary source |

`rdb5` and `vwap` read `vwap` and `trade_count` from the `history_load` extension's `bar_extras` file beside
the bars (bar type, `ts_event`, volume, `vwap`, `trade_count`); without it they are a named refusal.

Not registered. Signed flow: the adapter's trades carry no aggressor (F1 finding 1). Uptick share: no
sourced rule and no print history. Rows F1 names without a fixed rule: acceleration, trade-count
acceleration, pullback, down volume, tight range, strength against SPY, realised volatility, MAD z and
pre-market leadership. Close location, which uses the day's final values. Anything needing print conditions,
quotes, float, short interest, borrow, options, news, filings or trading status. Each needs a record of its
own.

**Arms.** `F-all` enters every event at `T`. `F-<id>` enters an event whose value is in the factor's top
group: 1 for a binary factor, 3 for `streak`. For `move`, `rdv`, `gap`, `pmadv` and `rdb5`, it is a value at
or above the nearest-rank 67th percentile of the known values of the training events, all parts pooled. An
unknown value is not entered. An arm with no training entries is not chosen in that fold. Each arm's
entries are replayed on their controls at the same minute, an event without one left out of that figure.

**Fill and outcome.** An entry at `T` fills at `T`'s close plus the half-spread. It is held to the close of
the first bar with `ts_init` at least 30 minutes after the entry, or the part's last bar if sooner, and sold
there at that close less the half-spread. The net outcome is sold / entry − 1. The half-spread is one
fractional rate per symbol, ADR 0092's, applied to both fills and keyed by symbol, never by clock minute. A
symbol without one, a control among them, takes the median over the unique symbols in its `tier`, with the
tier read at the event's reference close. The donors are the symbols of the events a fold chooses on, its
earlier blocks, for both its choice and its judged block; at a holdout opening, those of every development
event. A tier with no symbol to take it from is a named refusal, counted and left out of the cost figures.
ADR 0092's managed exit is not used. Outcomes go to a DIAGNOSTIC result file, not to the ledger
(`ledger.record_trial`), which ADR 0010 keeps for ranked variants.

**Folds and holdout.** ADR 0092's walk-forward on its development dates, its blocks and folds unchanged. In
each fold the arm with the largest median net outcome at the half-spread on the earlier blocks is judged on
the next block (ties to `F-all`, then the table's order). No fold or cut reads ADR 0092's holdout dates;
a holdout cut is the 67th percentile of every development event. Which holdout, by the lead's decision of
2026-11-11: if this record is on `main` before ADR 0096's holdout opens (09:30 New York on 2026-11-12 at
the latest), `F-all` and the seven bar factor arms (`move` to `repeat`) are registered arms of that opening,
on ADR 0092's holdout dates. Their replay runs once the factor computation lands, on those same dates, with
nothing about an arm changed after any result of that opening is read. They are reported beside ADR 0096's
selection, not in it, and its tie order is unchanged. `rdb5` and `vwap` go in every case, and the other
eight if this record is not on `main` before the opening, to this record's later holdout: the first 20
regular sessions after 2026-11-11 (the calendar UNVERIFIED), loaded as bars through `paper:history`, its
dates entered here before any is read, and opened once for the arms that take it. It is not ADR 0096's
later holdout, which is drawn from recorded sessions.

**Departures**, all this record's: three parts and controls where ADR 0092 has one window; entry at `T`
with no rising run, up to three a day; a 30-minute hold and no managed exit; selection by median net
outcome, not ADR 0092's share entered not late; every arm replayed at its opening, not one; at ADR 0096's
first opening, a replay after its other results are read, outside its selection, against ADR 0092's single
opening and ADR 0096's "opens once"; `rdb5` and `vwap` sent later even if `vwap` is ready at the opening,
against ADR 0096's "every arm whose inputs cover"; and a later holdout of this record's own, a third opening
of bars after 2026-11-11.

**What counts as a result.** For every arm on the holdout, at the half-spread and at twice it: the entries,
the mean and median net outcome, and the median minus `F-all`'s and minus the controls'. A factor is
**separating** only if, at twice the half-spread, it has at least 30 holdout entries, a positive median, and
a median above both `F-all`'s and its controls'. The 30 and the percentile are UNCALIBRATED. Every figure is
a DIAGNOSTIC (ADR 0092). `tier` gives each arm's outcome by stratum. The owner's labelled days are reported
only as recall of the population. Before ADR 0092's holdout dates are opened, recall and the factor run's
counts are reported for development dates only, and recall for the three daytime parts only; F2's artifact,
which spans the whole window, is hashed and not read for those dates, and overnight gaps are counted only as
discarded.

### Consequences

* Good: the bar factors are fixed before any is computed, with controls and `F-all` to check them.
* Bad: ten arms invite the loosest to win a fold; the controls and the `F-all` difference show it.
* Bad: a replay after the opening's other results relies on nothing being changed; the arms are fixed here
  and the run names this record's commit. The factors F1 found most promising wait for data.

### Confirmation

* Before any factor is computed, this record and PR #274 are on `main`; the run names their commits.
* F2's module runs with `--start` and `--end` at ADR 0092's window; its artifact's SHA-256 is entered here.
* The run names the opening each arm took. Later-holdout dates are entered here before any is read.

## Evidence

Read 2026-11-11. The factors' selection, cuts, tiers and the result rule are this record's choices.

* F1, a Codex reader's factor inventory, 2026-11-11 (96 rows, five findings; outside the repository): the
  rows used above and their source labels; finding 1 (no aggressor on the adapter's trades) and finding 2
  (`parse_bar` keeps no `vwap` or `trade_count`). Read; its primary sources not re-read here.
* F2, a Codex builder's `src/canon_trading/mover_population.py` and its tests (not on `main`; they land with
  the factor computation), on base `ac0098e`: 28
  passed when reproduced in a clean worktree on `main`, 2026-11-11. Its parts, prefix projection, control match and
  load plan are read from its source and report.
* ADR 0092 (PR #274, merged as `1e790c7`) and ADR 0096 on `main`: the registered listing condition,
  the reference close, the bar model, the costs, the split and walk-forward, the holdout and its later
  form. Read.
* https://docs.alpaca.markets/us/reference/stockbars — historical bars carry `vw` (vwap) and `n` (trade
  count) beside OHLCV, the inputs `rdb5` and `vwap` wait for. Fetched by the
  evidence-verifier, 2026-11-11.
* evidence-verifier on this record, 2026-11-11: two source labels (`streak`, `rdb5`), the uptick reason,
  F1's bar rows left unnamed, the overnight claim, five undeclared departures and twelve undetermined choices
  (net outcome, exit bar, half-spread by part and for controls, percentile, unknowns, an empty arm, the
  controls' figure, the test's cost). Answered above. A second pass, on the opening clause the lead
  decided later the same evening: the eights' meaning, the drivers' wording, one later holdout named the same
  way in both cases, the deadline as the opening itself, and three departures stated. Applied above.
* An Antigravity reader of the factor computation, 2026-11-11 (six findings; outside the repository):
  finding 1 (recall and the gap count read holdout dates and the overnight part) and finding 2 (cost donors
  pooled every development block, later ones included) led to the two sentences above; findings 3 to 6 are
  on the code, and 6 confirms the tier rule for controls. Read.
* evidence-verifier on those two sentences, 2026-11-11: narrowed "every count" to the factor run's counts,
  named ADR 0092's holdout dates, and corrected this bullet. Applied.
