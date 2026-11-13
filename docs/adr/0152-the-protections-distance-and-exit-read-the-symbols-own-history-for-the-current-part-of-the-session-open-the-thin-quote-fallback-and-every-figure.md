---
status: proposed
date: 2026-11-13
decision-makers: the lead (ruling 11, 2026-11-13, as the coordinator relays it); master orchestrator (Claude) for the record, under the owner's delegation (ADR 0074)
review-by: 2027-02-11
---

# The protection's distance and exit read the symbol's own history for the current part of the session: its one-minute range, its quoted spread and its printed volume a minute; open: the thin-quote fallback to the fixed 2% and every figure

## Context and Problem Statement

The owner objects to a static 2% stop. The default arm, OWN1, plans a long's trailing distance from
the symbol's own one-minute ATR, raised to two quoted spreads (`session_window.py:541-550`,
`mover.py:994-996`). In the Mover's entry plan, though, a quote with no usable spread at arming (a
missing or zero bid, or a crossed or locked quote) selects the fixed plan, the `trail` default of 0.02,
even when the ATR is warm (`mover.py:989`, `:1702-1709`, `_entry_trail` `:5530-5539`). That plan stays
fixed for the holding's life: only a cold-ATR fallback is planned again once warm (`:5838`), and the
ratchet skips a fixed bound (`:1733`). The protect task refuses the same case instead
(`protect.py:406-409`). Thin quotes come with the extended-hours parts (`UNVERIFIED` here: no source
read for it), so the static distance comes back where the owner's objection bites hardest.

On 2026-11-13, an after-hours carry window on paper account B ran a branch's carry, not `main`'s
(main has no caller that arms a carried long; ADR 0150, clause 6). The window's lane confirmed from its
logs, as relayed:
- both carried longs took the fixed fallback, with no quote at arming, though each had one-minute
  bars enough for an ATR;
- on one, the fallback narrowed the distance it had been carried with by about two fifths, and that
  long sold 29 seconds after arming;
- the other's distance widened slightly.

The highest trade close since the protection started was above the highest bid over the same span.
So a level rebuilt from closes for a bid-triggered trail sits on another price series than its trigger.
The lead ruled the same day that a rebuilt level uses the live trigger's price series: bids for a
bid-triggered trail, closes for a last-price one. A carried long carries its level and distance, and
the level is raised only by bids.

What the protection reads already depends on the part in one place, and not in others:
- the Mover's cached ATR warm-up is bounded to the part (`_warm_atr`, `mover.py:3746`, `:3756`;
  ADR 0133);
- the Mover's requested history (an hour, `history_window` `:1500-1507`) and the protect task's
  60-minute read (`protect.py:172-174`, used at `:373`) are not;
- the spread is the latest quote's;
- under OWN1, printed volume enters only the entry's size (`flow_share` of the last `flow_bars` bars'
  dollar volume, `mover.py:20`, `:684-691`), though the tier and profile bases read a baseline of
  dollars a minute (`:5549-5576`, `:5820-5824`).

## Decision Drivers

* The owner's requirement: no static distance, and every holding protected in every part (ADR 0150).
* Upstream first: use what a maintained framework documents, and name as ours what none does.
* No figure is invented: each threshold here is `UNCALIBRATED` until a registered replay sets it.

## Considered Options

1. Keep the fixed 2% fallback for an unusable spread, and keep the ATR without a part bound.
2. Plan from the symbol's own history for the current part of the session, using three inputs:
   - its one-minute range (ATR over that part's bars);
   - its quoted spread, taken over a span rather than from one quote;
   - its printed volume a minute.

   An unusable latest quote falls back to that history, not to a fixed figure, and each input is read
   on the same price series as the trail's trigger (bids for a bid-triggered trail).
3. Plan from an upstream risk model as it stands, such as LEAN's per-security trailing drawdown.

## Decision Outcome

Chosen, proposed: **option 2**. Option 1 is the static distance the owner rejects, and it returns in
the thin parts. Option 3 does not avoid it either: LEAN's trailing-stop and per-security drawdown
models each take one fixed `maximum_drawdown_percent` (default 0.05), calibrated per symbol by
nothing.

**What upstream covers, and is used as documented:**
- a participation cap: LEAN's VWAP execution model caps each order at 1% of the current bar's volume
  (`maximum_order_quantity_percent_volume`). Canon's entry cap is modelled on it, at 5% over five bars
  by default, `UNCALIBRATED`. Whether LEAN's model caps sells as well as buys is not read here;
- slicing an order over time: NautilusTrader's TWAP execution algorithm (`horizon_secs`,
  `interval_secs`; the first slice at once, the rest at each interval). At the pin (rc8) the native
  TWAP is registered by name only on the backtest engine
  (`crates/backtest/src/python/engine.rs:468,1121-1136`). The live node's `add_exec_algorithm` takes an
  instance, and no native TWAP for it was found, so its use for a live exit is open;
- the ATR itself: NautilusTrader's indicator at the pin, as ADR 0088 uses it.

**What no upstream covers, and is ours, `UNCALIBRATED`:**
- the gaps:
  - a calibration by part of the session;
  - a distance planned from the quoted spread;
  - an exit's distance from printed volume a minute on a small cap, and a cap like LEAN's applied to
    a protective sale;
- the evidence on those gaps: no source read sets a stop from the spread or by part of the session,
  and the ATR trailing stops read (Freqtrade's 2 ATRs, StockCharts' 21-period ATR times 3 and its
  Chandelier Exit's 22 times 3) agree on no single lookback, multiple or bar size;
- the three inputs, read for the part the holding is in:
  - the one-minute range (ATR over that part's minute bars, bounded to that part);
  - the quoted spread (a statistic over a span of two-sided quotes, not the latest one; both open);
  - printed volume a minute (the part's traded volume over its minutes).
- Each figure is a registered row, `UNCALIBRATED`: the span, the multiple and the participation share.
- An unusable latest quote keeps the history's spread for that part, and so does not fall to the
  fixed trail. If the part has no two-sided quote at all, the holding is named unprotected under
  ADR 0150's clause 2 rather than given a fixed figure.

**The thin-quote fallback, recorded tonight as a limit of the default arm on `main`:** in the Mover's
entry plan, an unusable spread selects the fixed 2% plan whatever the ATR (`mover.py:5530-5539`). The
after-hours window above showed the same on a branch's carry (relayed from its lane's logs; that the
branch shares `main`'s plan is `UNVERIFIED`). No code changes tonight (the lead's ruling); the unit that
builds option 2 removes the fallback with a failing test first, on a shaped quote tape whose spread
goes unusable at arming.

### Consequences

* Good: the distance follows the name and the part it trades in. The thin parts get the wider
  distance their own history shows, not the fixed one.
* Good: the Mover already subscribes to the three inputs (minute bars, quotes, trade prints;
  `mover.py:3781-3783`), so no new feed is needed there. The protect task reads only the latest quote and
  bars.
* Bad: three more `UNCALIBRATED` rows, each needing a registered replay before it can be called
  calibrated (ADR 0092).
* Bad: an extended-hours part may have too few bars or quotes for its own history early in the part.
  Whether the previous like part is used then is open.
* Open:
  - each figure;
  - the spread's span and statistic;
  - whether volume a minute sets the distance, the exit's slices, or both;
  - Alpaca's paper fills, which are not checked against the quoted size ("Your order quantity is not
    checked against the NBBO quantities"), so a paper exit cannot show what the book absorbs.

### Confirmation

* The unit that builds option 2 names this record and ADR 0150's clauses 1 and 3. It removes the
  fixed fallback with a failing test first, on a shaped tape whose spread is unusable at arming while
  the part's history is warm.
* The two after-hours cases of 2026-11-13 are the first entries in that test's case set once their
  facts are confirmed from the window's logs.
* A registered replay per part sets each row before any is called calibrated.

## Evidence

Read 2026-11-13. A scout re-fetched each upstream page today. Its fetches were summarised by a small
model, so quotes are its rendering.

* `src/canon_trading/mover.py` on `main` — read:
  - `:989`: the `trail` default 0.02, the fixed plan; `:559`: `TRAIL_CAP` 0.02, the cap on other plans;
  - `:994-996`: `atr_k` 1, `atr_n` 14, `spread_floor_m` 2;
  - `:1702-1709`: `quoted_spread`, under which a missing or zero bid, or a crossed or locked quote, is no usable spread;
  - `:5530-5539`: an unusable spread returns the fixed plan with the note "no usable quoted spread ... fixed trail fallback";
  - `:20` and `:684-691`: `flow_share` on the entry only, modelled on LEAN's VWAP cap; `:5150-5153`,
    `:5306`: its use in `_buy`;
  - `:1733`, `:5838`: a fixed bound is never planned again; `:3746`, `:3756`: the cached warm-up bounded
    to the part; `:1500-1507`: the requested hour; `:5549-5576`, `:5820-5824`: the tier and profile bases'
    baseline dollars a minute; `:3781-3783`: the bars, trades and quotes subscribed.
* `src/canon_trading/session_window.py:541-550` on `main` — read: the arms F2 (fixed), OWN1 and OWN3
  (ATR, cap arm `own`), and OWN1 as the default.
* `src/canon_trading/protect.py` on `main` — read: `:172-174` and `:373`, the ATR warmed from the last 60
  minutes of bars, `UNCALIBRATED`, with no part bound; `:406-409`, an unusable spread refused.
* https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/execution/supported-models.md
  — "It sets a maximum order size of 1% of the current bar's volume", adjustable through
  `maximum_order_quantity_percent_volume` — fetched.
* https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/risk-management/supported-models.md
  — the trailing-stop and per-security drawdown models take one `maximum_drawdown_percent`, default
  0.05 — fetched.
* https://nautilustrader.io/docs/md/latest/concepts/execution/algorithms.md — the TWAP algorithm's
  `horizon_secs` and `interval_secs`: "`horizon_secs` must be at least `interval_secs`", with the first
  slice at once and the rest at the interval — fetched. That it ships in the pin's Python package is
  `UNVERIFIED` here.
* https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/strategy-callbacks.md — "Use a
  different stoploss depending on the pair", "The stoploss price can only ever move upwards", and a trail
  "at 2xATR below current price" — fetched.
* https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/atr-trailing-stops
  — "By default, the ATR Trailing Stops are calculated using a 21-period ATR" and "the multiplier is set
  to 3", with no bar size named — fetched.
* https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/chandelier-exit
  — 22 periods and a multiplier of 3 — fetched by the second reader.
* NautilusTrader at the pin (`scripts/canon src nautilus-trader`):
  `crates/backtest/src/python/engine.rs:468,1121-1136`, the native TWAP registered on the backtest
  engine — read by the second reader. The TWAP page above is the latest docs, and rc9 is out.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — extended hours accept limit orders only, and
  "Trailing stop will not trigger outside of the regular market hours" — fetched.
* https://docs.alpaca.markets/us/docs/paper-trading.md — "Your order quantity is not checked against the
  NBBO quantities" — fetched.
* The lead's archive: five scout reports of 2026-11-13 — read; each upstream they cite was re-read for
  the lines above.
  - The per-symbol calibration, session-boundary and liquidity report found no source that calibrates
    a stop by part of the session or sizes a position against recent traded volume.
  - The trailing-distance, 24/5 protective-stop, gain-protection and live-volume-lists reports found
    none that sets a trail from the quoted spread.
* The after-hours carry window on paper account B, 2026-11-13, on a branch's carry: its lane's reading
  of the logs, as relayed by the coordinator. Both longs were on the fixed fallback with no quote at
  arming. One's distance narrowed by about two fifths and it sold 29 s after arming; the other's widened.
  The highest close since the start was above the highest bid. The log is kept outside the repository,
  and these are the lane's figures, not re-read here.
* The lead's ruling of 2026-11-13, as relayed: a rebuilt level uses the live trigger's price series, and
  a carried long carries its level and distance.
* Second reader (`evidence-verifier`), 2026-11-13: 10 of 21 claims held, 9 in part, 2 failed. Its
  corrections are made above:
  - the cached warm-up is bounded to the part;
  - the fixed fallback is the Mover's alone, and the protect task refuses;
  - printed volume enters the tier and profile bases;
  - the fixed default's line, and the exact line ranges;
  - LEAN's cap modelled at 5% over five bars, not followed at 1%;
  - the live TWAP is open;
  - the spread's statistic is open;
  - the incident labelled.
