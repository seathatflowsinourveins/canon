---
status: proposed
date: 2026-11-11
decision-makers: the owner for the 30-minute horizon (2026-11-11); the lead, under the owner's delegation (ADR 0074), for the split by data source and the holdout's one opening (2026-11-11)
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
* Amendments 10 and 11 to this record (the `profile` trail arm and the cap policy) are not on `main` at the
  freeze; their draft's SHA-256 is `f5f8ac1737ccb9aef76ee3a8fd6ec398130ac369b7e9d2e45c987cb6804f2241`. That
  draft applies on top of amendments 1 to 9, also not yet on `main`, which land with it in one unit. It
  lands byte for byte as hashed here, or the opening is void for the arms it covers.

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

## Amendment, 2026-11-12: the opening is a freeze at 09:30 and one read later

The lead's decision at about 05:45 New York, before any holdout outcome is read. This record said the holdout
opens once, at 09:30 New York on 2026-11-12 at the latest. At about 05:40 the study command's inputs were
found not to exist: no name-day list of the population (`early_entry_study --days-file` takes
`YYYY-MM-DD,SYMBOL` lines and their registered SHA-256, and splits that one list 70/30 itself), no registered
hash for one, and no catalog root holding both windows (the development sessions are in one root and the
holdout's in another, and the command reads one `--catalog`). So the opening is two steps.

* **The freeze, on `main` before 09:30.** Its time of record is the merge time GitHub sets on its pull
  request (`mergedAt`), not a local commit time; if that comes out after 09:30, the freeze says so and the
  opening is late, and either way nothing registered after the freeze's content is admitted. It fixes
  `main`'s commit; the SHA-256 of this record as committed there, with its amendments, and of the draft of
  any amendment to it not yet on `main`, each named in the freeze; the holdout's 18 dates in
  `canon/holdout-adr0096.txt` and their SHA-256, derived from ADR 0092's rule (the 60 regular sessions
  before 2026-11-11, 2026-08-18 to 2026-11-10, split in order 42 and 18); every admitted arm with the commit
  that registered it, with ADR 0100's `F-all` and the seven bar factor arms as registered arms with replay
  pending at `0e8542fe29d823d5096563959b7f9d3234bf870e`; every registered arm left out, with its reason;
  and the statement that no process has read an outcome on a holdout date, with what has read holdout bars
  and why. Nothing registered after the freeze's commit is admitted to this opening.
* **The read, later and exactly once,** when the population's `YYYY-MM-DD,SYMBOL` list exists for all 60
  sessions by ADR 0092's rule (its SHA-256 then recorded in a second entry that names the freeze) and one
  catalog access covers both windows. The read checks the freeze's hashes first and refuses on any
  mismatch. The study splits on the dates its list holds, so 42 and 18 hold only if every one of the 60
  sessions carries at least one name: the read refuses, by date, a session with none. Until then the
  development replays read no holdout date, guarded by the tracked day list and a test, whichever root
  they read.

Where this record says an arm joins if "its inputs cover the training dates at that moment", the moment
is the freeze: an arm not ready then, such as the bar-derived rows, waits for the later holdout. Where
this record's Confirmation and ADR 0100 say "before the holdout opens" or "before it opens", that now
means before the freeze.

Nothing was read: no holdout outcome had been computed or seen when this was decided. The development
population build, still running, is an input to the read, not to the freeze.

## Amendment, 2026-11-12: the freeze's stamp, amendments 1 to 11 as tracked drafts, and when text is registered

* **The freeze.** `canon/holdout-adr0096-freeze.json` landed as pull request #295, merge commit
  `a76622148056be06ce1f44cb73696d3ef0014217`, merged by GitHub at 2026-11-12T13:31:24Z (08:31 New York),
  before 09:30. It names `main` at `7b39a1afdfba96a30bdafe7f2f9e8484b8520491`, whose files it hashes; the
  one commit between that and its merge (#294) changes none of them.
* **Amendments 1 to 11, as drafted.** The text the freeze binds (its admitted and pending arms' reasons, and its
  commit's message, name both hashes; its own `amendments` hash is of this file at `7b39a1a`) is tracked in
  `docs/adr/0096-amendments/`,
  byte for byte as hashed: `adr0096-with-amendments-1-9.md`, this record with amendments 1 to 9 as last
  written on 2026-11-12 at 02:11 New York (SHA-256
  `6cddb5d2c0d74152fb40d992a7be2c78506b53c0b5b72a7f49528760816dd556`), and `adr0096-amendments-10-11.diff`,
  amendments 10 and 11 as a diff that applies on top of it (SHA-256
  `f5f8ac1737ccb9aef76ee3a8fd6ec398130ac369b7e9d2e45c987cb6804f2241`). They are kept as written, a
  working label included, because byte-identity is what the freeze binds. Their front matter and their
  place differ from this file's, so they are an appendix to it, not merged into its text; they bind as
  this record's amendments from this landing.
* **When text is registered** (the lead's standing rule, 2026-11-12): an arm, a base or a clarification
  is registered when its text is hashed and the hash is written, with its time, in a tracked file or the
  coordinator's dated digest, in the same step. Text that exists only in a worktree is a draft.
