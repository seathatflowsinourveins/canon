---
status: proposed
date: 2026-11-12
decision-makers: repo owner (requirement, 2026-11-11: a breach must persist before the sale); lead orchestrator (Claude) for the rule, the gap, no expiry and the carrier
review-by: 2026-12-12
---

# A breach is the venue's own election, a consolidated print not outside the quote, and here also a second print after a gap (under the default arm two spaced two-sided quotes, amended 2026-11-13), the stop held by the strategy and off by default; open: the quote test, a paper window, the protector's use of it, the gap's value and the silent tape

## Context and Problem Statement

The engine's emulator elects a protective stop on one tick. On 2026-11-12 one observation elected or
would have elected a sale in three ways: one venue's top-of-book bid far under the market, one order's
sweep of the book, and one stray print (recorded tapes kept outside the repository: Evidence). ADR 0088 named "a breach that must persist" as a departure not
built. Alpaca states how it elects a stop it holds itself. Which rule confirms a breach here, and
which part carries it?

## Decision Drivers

* The venue's own contract is the base: a stop here is not elected by a print its stop would ignore.
* One observation never sells: not a quote from one venue's book, not one order's sweep, not one print.
* A stop that cannot fire on a thin name is a hole, not a limit (the lead, 2026-11-12).
* Upstream parts as shipped; what has none is named and kept small (rule 3).
* One rule for the Mover (ADR 0088) and the protector (ADR 0105). Every number a registered row,
  UNCALIBRATED.

## Considered Options

Each is a way to add to the base, which is not an option: Alpaca's contract for its own stops.

1. Nothing added: the emulator's election on the last trade (`LAST_PRICE`), one print.
2. Upstream's `DOUBLE_LAST` and `DOUBLE_BID_ASK` trigger types.
3. An emulated order that takes its trigger from a second instrument carrying a filtered tape.
4. The strategy holds the stop and confirms on a count of spaced prints.
5. The bid staying under the level for a minimum time.

## Decision Outcome

**The base.** Alpaca's stops "are elected on the consolidated print. Your sell stop order will only
elect if there is a trade on the consolidated tape at or lower than your stop price and provided the
electing trade is not outside of the NBBO". A breach here is at least that: a consolidated print at or
under the level, not outside the quote. The first half is the emulator's `LAST_PRICE` on the
consolidated feed. The second half, the test against the quote, is not built: the first gap below.

Two things are kept apart. Which prints become ticks at all is the adapter's condition filter, a
setting that is off by default and its own record's. Which prints may elect a stop is the venue's rule
above and, **added to it** here with its own rows, a second print after a gap. Whether an odd lot
elects Alpaca's stops its page does not say: UNVERIFIED. Here an odd lot counts, to confirm and to
reset, one counted set for both directions (the lead, 2026-11-12): with the gap on, leaving odd lots
out removed nothing measurable and delayed real falls. A print the filter drops for a reason other
than its size stays out by default, the filter's own row.

Chosen for the count: **option 4**, `proposed` until the gaps below close; off by default, so option 1
is what runs. Option 2 "Requires two consecutive matching last prices", but the emulator cancels an
order with either type, and at the pin only the Interactive Brokers adapter carries them. Option 3 is
UNVERIFIED and a larger change; rc9 lists a fix for emulated orders that share a trigger instrument, so
it is not tried at rc8. Option 5 is refused on one venue's feed and unmeasured on a consolidated quote.

The count, with `breach_count` or `high_count` above one, on trade prints only (`LAST_PRICE`;
`BID_ASK`, `protect=False` and any `trail_mode` but `fixed` are refused by name):

| Part | What it is |
|---|---|
| A counted print | One at least `confirm_gap_ms` after the last that counted. One order sweeping the book prints dozens of trades with distinct nanosecond times inside a millisecond (a recorded tape, outside the repository): one observation. |
| A breach | `breach_count` counted prints at or under the level with no print above it between them. The count never expires: a thin name sells at its second print however late it comes. |
| A new high | `high_count` counted prints above the confirmed high, none at or under it between them; the level rises to the lowest of them less the trail, on Alpaca's price step, never down. |
| The level's start | The entry's average fill as it stands at the arming step, less the trail; never a cached print. The start is not re-based as a partly filled entry fills further; the trail's distance is, being taken from the current average each step. |
| A print's time | The earlier of its event time and its receipt, the receipt read on the venue's clock when its name's quotes show this host's clock behind (the highest of the name's latest nine quotes' event time less receipt, the least-delayed quote's). A print with the last print's time may reset an arm and never counts; an older one, a late report, does neither. |
| The sale | A DAY limit `exit_ticks` through the bid of the first step after the confirming print, so it never rests above the bid as the emulator's released limit can; then the existing chase. Its floor is `chase_cap` under a first price taken from the lower of the confirming print and the bid at that step. A one-sided quote's zero bid is no price, and neither is no quote: the last trade then stands in for the bid, for the floor and for the sale and each remake of it, so a recovery cannot lift the sale and a fall under the print is followed. The last trade is the one newest by a print's time as the row above reads it, not the one received last. The floor is set once; released after its wait, it stays released. The exit's record is marked at the confirming print's own time and price, as a stop-out. |
| The rows | `breach_count` 1 and `high_count` 1 (both off), `confirm_gap_ms` 100. The on row is 2, 2 and 100 ms. |

**Gaps, and what would close each.**

* **The quote test is not built**, so a print the venue's own stop would ignore counts here. Closed by
  its own change, test first on shaped tapes that carry quotes: a print under the level and outside
  the quote does not count, one at the bid does; consolidated feed only; with no quote newer than a
  stated age, a row, the count alone applies and the record says so; off until a paper window has
  shown it. The protector does not elect on prints again before it (the lead, 2026-11-12).
* **No paper window has run it**, and none can: the trigger has a window flag (`--trigger`), but the
  count rows have none yet (the selection unit). Closed by that flag and a window whose record names
  the rows.
* **No print trigger overnight.** The trade-condition filter a print trigger turns on is read for
  sip's tapes A, B and C only, so an overnight part, which streams boats, refuses `--trigger
  last_price` by name, and a full window on the bid is refused (the lead, 2026-11-12): account B
  stands idle from 20:00 to 04:00. Closed by an overnight measure on paper, for the names a ranking
  picks, of how often each prints on boats and its longest gap (a print trigger sells nothing while a
  name does not print), of the condition codes boats prints carry and which of them Alpaca's boats
  minute bars take, and of how often a boats quote is one-sided.
  * 2026-11-13: account B need not stand idle overnight, and overnight protection runs on the `boats` bid;
    shown so far by hand. At `05905849` the overnight part streams `boats` (`session_window.py:2358`,
    `MOVER_PARTS`), the Mover's trailing sell is elected by the bid of every quote by default (`bid_ask`,
    `:759-767`), and a print trigger is refused there by name (`:3043-3047`). The schedule's overnight
    entries: each scheduled firing runs `--session auto --mover`, the Mover in every part, overnight
    included (`scripts/automation:307-311`, its arguments at `:338-342`), on account B (#252), while
    `canon/paper-schedule` reads `mover`; overnight ranks on dollars with no baseline (`:316`,
    `session_window.py:1792`). No scheduled firing has run a Mover window overnight: on the night into
    2026-11-12 the firings placed nothing (no overnight Mover form yet, a held lock, or a skip), the
    schedule was off from #289 to #350, and the next overnight part opens Sunday 2026-11-15 at 20:00 New
    York; that first scheduled overnight window is `UNVERIFIED` on paper. On the night into 2026-11-13, by
    hand on account B on `boats` at commits from `bf5742e0` to `efd37edb`, thirteen Mover windows from 22:01
    to 03:40 New York ended `ok`, and three before them were stopped by a signal. Whether the idle line was
    a ruling never taken up or one withdrawn is not established.
* **The protector does not use it, and the Mover only on the fixed trail.** A trail plan's distance,
  its tighten and its chase cap (ADR 0088's `trail_mode`) are
  not carried into the held level. Closed by that change in each, with its tests.
* **The gap's value.** Three tables over recorded tapes of one day bound it and do not set it. A
  count of two sold every one of 25 events that sold or fell, at 100 ms and at 250 ms. A 2% stop
  walked along those tapes and re-armed was elected 2,215 times by a bare print, 95% of them
  recovered within 10 s, and 835 times at the on row, 81% recovered, a median 0.16 s later. A
  recovery does not prove a sale wrong. Closed by paper windows run with the arms on.
* **The silent tape.** After one print under the level a tape with no further print holds; only a
  consolidated bid covers that. In one thin name on 2026-11-12 the longest gap between prints was
  17.7 s, and 47.6 s among those that qualify (another session's measurement, UNVERIFIED here).
* **The window's protection diagnostics** read sell orders, so a holding under an arm reads as
  unprotected for its whole life. Closed before a window turns an arm on.
* **The two clocks.** NautilusTrader at the pin corrects no clock offset, so the reference is ours:
  a name's quotes give the venue's time as the stream shows it, and a print more than 5 s ahead of
  it has no usable time. With no quote of the name, the receipt stands in and the run record counts
  each print so judged (`prints_timed_on_receipt`). With this host's clock behind, a print received
  faster than the name's quotes stamps up to that difference before its venue time. The record
  carries the clocks' difference (`host_clock`), and a window more than 2 s apart goes to the lead.
  The adapter's own 5 s bound, which lands with its part, measures against receipt and is to move to
  the same reference. A name whose nine latest quotes all run more than 5 s behind its prints sets
  its prints aside as ahead until a timely quote comes; `prints_stamped_ahead` counts them.

### Consequences

* Good: a sweep, a stray print and a quote no longer sell by themselves; a thin name still sells.
* Good: with both arms off the orders are those of before, and a trial's key leaves the three rows out,
  so a configuration keyed before them keeps its key. A run's record does gain `persistence_arms` and
  the three rows, on or off.
* Bad: the sale leaves at most `STEP_SECS` (1 s) after the confirming print, where the emulator acts
  inside the tick. A print before the arming step is never read. With no bid in the cache the sale is
  priced from prints alone, the lower of the confirming print and the last trade, which is the newest
  by its own time and not the last one received, so an older print reported late moves nothing; before
  2026-11-12 it was priced from a one-sided quote's zero bid, at the lowest tick, which no window ever
  ran.
* Bad: the count and the rising level are our arithmetic, not upstream's; a pin move does not cover them,
  and the bars replay, which delivers no print, refuses an arm by name and cannot rank it.
* Bad: no order exists anywhere while the strategy holds the stop. A dead or sleeping node protects
  nothing, as with the emulator, and nothing rests at the venue to show it.
* Bad: on a fast fall the sale follows the bid below the stop the entry was sized against; and with
  only the breach arm on, one higher print lifts the level at once. The on row turns both arms on.

### Confirmation

`tests/test_mover_breach.py`, 50 cases; 37 run a real `BacktestEngine`, 13 only build a configuration,
a record, a key or a replay strategy. Every tape is shaped: an invented instrument, offsets for times. At the on
row a sweep of 28 prints inside 1.2 ms through the level holds, and sells with the gap at 0; a large
print 0.94% under the market with a second 4 ms later and prints above between and after holds; a
two-sided quote 2.3% under every print holds. A sweep one step above the level holds too, which shows
the tape and not the rule. Two prints under the level 60 s apart sell at the second. A sustained drop
sells by DAY limit with no sell of any status before the confirming print, 700 ms after it on that
tape and 900 ms on the thin one. A gap from 10.35 to 10.00 through a 10.29 level sells on the first
limit, 9.98 under a 9.99 bid, 700 ms after the confirming print, with no chase. `tests/test_tuning_study.py` holds one more: a trial keyed with both
arms off holds no persistence row and decodes as off. Run in `scripts/ci`.

Not reproducible from the repository, so UNVERIFIED for a later reader: all 40 of the first cases
failed before the code existed; mutating the gap, the reset, the count and the high's lowest-of-N
each failed named cases; the seven cases of the two review rounds, and the three of the trail-mode
refusal, failed before their fixes; a default configuration's key
fields were byte-identical to those on `main` before the change. On paper: UNVERIFIED.

## Evidence

Read 2026-11-12. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records.

* NautilusTrader at the pin, `nautilus-trader@d53849b216e2`:
  `crates/execution/src/order_emulator/emulator.rs:598-606`, only `Default`, `BidAsk` and `LastPrice`
  are emulated and any other trigger cancels the order; `:1076-1104`, each quote or trade tick is
  matched as it arrives; `crates/execution/src/matching.rs:555-572`, with the comparison at `:426-436`
  and `:583-599`, one price and no count or time; `crates/model/src/enums.rs:2221-2223`, the two double
  types; `docs/concepts/orders/index.md:221-222`, "Requires two consecutive matching last prices";
  `crates/adapters/interactive_brokers/`, the only adapter that names `DoubleLast`;
  `emulator.rs:612-650` and `:704-714`, the trigger instrument and its subscription;
  `python/nautilus_trader/trading/__init__.pyi:638`, `on_trade` — resolved.
* NautilusTrader v2.0.0rc9, a local copy at `da6b57aefdd6`, the commit that tag resolves to; not the pin:
  `crates/execution/src/order_emulator/emulator.rs:564-568`, the same three triggers and no other;
  `RELEASES.md:188`, "Fixed emulated orders sharing a trigger instrument triggering from the wrong quote
  or trade source" — read.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "Stop orders and trailing stops are elected
  on the consolidated print. Your sell stop order will only elect if there is a trade on the
  consolidated tape at or lower than your stop price and provided the electing trade is not outside of
  the NBBO" — fetched raw.
* Recorded tapes of three names on 2026-11-12 and a scratch table over them, kept outside the
  repository, so UNVERIFIED for a later reader; the tests' shaped tapes carry their geometry and no
  recorded row. A sweep of 28 prints with 28 distinct nanosecond times inside 1.2 ms, the next print
  above it 958 ms later. A print 0.94% under the two-second median, back within 15 ms, with two
  qualifying prints above the level between the two under it. One venue's top-of-book bid 2.3% under
  every consolidated print, for a millisecond and for up to 3.9 s. The table, the gap at 0, 50, 100, 250
  and 1000 ms by a count of 2 and 3: both false exits held in every cell, and the one crossing sold in
  every cell, 0.76 s to 4.6 s after the first qualifying print under the level. A second table over
  25 events with prints (15 exits, 10 falls), read in its report: the count of two confirmed all 15
  exits at 100 and 250 ms and all 10 falls; the falls' delay from the first raw print under the
  level was 0.11, 4.64 and 120.0 s (least, median, most) with odd lots excluded and 0.11, 2.11 and
  19.4 s with no exclusion; six of the ten first crossed on a print under 100 shares. Levels were
  fixed per event and often already breached at the tape's start. A third, read in its report: a stop
  2% under the running high walked along the same 25 tapes and re-armed after each election, with
  odd lots excluded: a bare print 2,215 elections, 95.3% recovered within 10 s and 97.4% within 60 s;
  a count of two at 100 ms 835, 81.0% and 90.4%, a median 0.16 s and at most 1.85 s after the bare
  print's election; with no exclusion a bare print 3,194, of which 1,336 on odd lots, and a count of
  two at 100 ms 876, 82.3% recovered within 10 s. On the ten falls of the second table, with no
  exclusion, the sale's delay was 0.11, 2.1 and 19.4 s; with odd lots out, 4.6 s median and 120 s
  at most: the basis of the ruling that an odd lot counts. That stop is not
  the Mover's, whose distance is taken from the entry; the tapes were chosen, not sampled.
* The lead's ruling of 2026-11-12 — an instruction, not evidence: the count of two, the gap, no expiry
  and the strategy holding the stop.
* `/code-review` at high effort, 2026-11-12, nine findings: six fixed with a failing test first (the
  bars replay unprotected under an arm, a print stamped ahead hiding later ones, the level seeded from
  one cached print, every configuration's key moved, an arm accepted with protection off, a dead
  branch); three stated above. The Mover's owner, the same day: a recovery stamped at a counted print's
  time did not reset, fixed with a failing test first; the breach-only row and the partly filled
  entry, stated above. evidence-verifier, the same day, on this text while it was part of ADR 0088:
  the two print-time rules, the floor's reference, the delay's two exceptions, what a record gains
  with the arms off, the counts of the cases, the labels, and the three sources added above. Applied.
* 2026-11-13, the overnight line: code at `05905849` read in the tree; the schedule's history from
  `git log -- canon/paper-schedule`; the windows from their by-hand records and the scheduled runner's
  log, kept outside the repository; the overnight part's hours from
  https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md ("8:00 PM ET Sunday to 4:00 AM ET
  Friday"), `fetched` by the evidence-verifier. Its pass corrected the line's cause for no scheduled
  overnight firing, tied the windows to their commits and counted the whole night. Applied.

## Amendment, 2026-11-13: the count moves to two-sided quotes under the default arm, a span joins it, and the protection part is its second carrier

**What this changes in the record above.** The count and the gap stay. What changes is their tape and their
arm: a breach is counted on two-sided quotes, not trade prints only, so the default paper window stops electing
on one quote. A span row joins the count and the gap. ADR 0119 stays `proposed`, and nothing is lifted in code
until this amendment is on `main` and the protection part's seam (ADR 0146) exists.

**The gap (AGENTS.md rule 3).** Upstream at both pins has no multi-observation or span trigger for an emulated
order. At `nautilus-trader@d53849b216e2` the emulator accepts only `Default`,
`BidAsk` and `LastPrice` and cancels any other trigger
(`crates/execution/src/order_emulator/emulator.rs:598-606`); each quote tick replaces the one bid and ask and
iterates the orders at once (`:1076-1091`); a stop matches one price, with no count or span
(`crates/execution/src/matching.rs:426-437`, `:555-580`, `:583-599`); release and submission follow in the same tick
(`emulator.rs:1267-1293`, `:1315-1416`). `DoubleLast` and `DoubleBidAsk` are enum values only
(`crates/model/src/enums.rs:2221-2223`), and only the Interactive Brokers adapter names them. The v1 pin has the
same guard (`crates/execution/src/order_emulator/emulator.rs:570-578`) and the same unimplemented doubles
(`crates/model/src/enums.rs:2019-2021`).

**What `main` refuses today.** `MoverConfig.__init__` refuses `breach_count` or `high_count` above one unless
the trigger is `LAST_PRICE`, `trail_mode` is `fixed`, `size_from_budget` is false and `protect` is true
(`mover.py:1137-1158`). The default window is none of those: OWN1 is `atr` over `own`
(`session_window.py:540-550`), the trigger is `bid_ask` (`:761`), the budget sizes the entry (`:1742`), and no
breach or high count is passed, so both are one. A default window therefore elects on one quote through the
emulator (a code reading of 2026-11-13, its first finding).

**The rule.** A breach is `breach_count` counted observations at or under the held level; a new high is
`high_count` counted observations above the confirmed high. Under the default arm an observation is one
two-sided quote: a positive bid and ask (`two_sided`, `mover.py:1712-1720`). It is stamped by the earlier of its
event time and its receipt, as a print is (`stamp_of`, `:1869-1876`). It counts only if it is at least
`confirm_gap_ms` after the last that counted, and the counted set must lie inside `confirm_span_ms`. A quote
whose bid is above the level clears the breach count; one at or under the confirmed high clears the high count.
A one-sided quote, or a crossed one (`bid > ask`), neither counts nor clears; a locked one (`bid == ask`) counts,
its bid being a price at which the holding can sell. A quote with the last counted quote's stamp may clear and
never counts; an older one does neither. The count and the high are held by the strategy, and the sale is a DAY
limit through the bid on the first step after the confirming quote, then the existing chase.

| Row | Value | Source | Status |
|---|---|---|---|
| `breach_count` | 2 | ADR 0119's on row, framed on consolidated trade prints | UNCALIBRATED on quotes |
| `high_count` | 2 | ADR 0119's on row | UNCALIBRATED on quotes |
| `confirm_gap_ms` | 100 | ADR 0119's on row, bounded by the 2026-11-12 tape tables on prints | UNCALIBRATED on quotes |
| `confirm_span_ms` | unset: no maximum until the measure sets one | this amendment; no measured source (ADR 0119's option 5 is the nearest, unmeasured on a consolidated quote) | UNCALIBRATED |

The counts are off at one (`mover.py:1013-1014`), the gap's 100 ms is inert while they are, and the span is unset, and none is carried into code before the amendment lands. The
span is the one row with no measured source; until it has one the count keeps ADR 0119's no-expiry, and every
exit records the span its counted quotes covered so the measure can set it.

**The carrier.** The Mover's held stop (`_protect`, `mover.py:3933`, and `_persistent_trail`, `:4069`) and,
once its seam exists, the protection part ADR 0146 opens, which must run the same state machine so a reconciled
holding gets the same rule. ADR 0146's gap is the part; this amendment does not build it.

**The measure.** A count per arm, never profit and loss: exits by the trailing stop, exits within 60 s of the
first fill, and re-entries after a stop. The arms are today's one-quote election, the count with the gap, and
the count with the span once it is calibrated. The first paper windows run the count and the gap with the span
unbounded.

**Related, handled elsewhere.** The same reading's second finding (the held stop's `chase_cap`, the whole-holding anchors and tightenings, and
reconciled holdings in `_protect` and `_persistent_trail`) is a fix for the record of the change that builds the held stop. Its third finding
(`protection_diagnostics` and `d3_check._a3` reading a strategy-held stop as unprotected) is another. Neither is
proposed here.

### Consequences

* Good: a default window no longer sells on one quote, and the Mover and the protection part share one rule.
* Good: the rows are named and off by default, and no code is lifted before the amendment lands.
* Bad: the span has no measured value, so the first windows run without it, with the count and the gap only.
* Bad: a sale leaves up to one `STEP_SECS` after the confirming quote (`mover.py:507`), where the emulator acts
  inside the tick.
* Bad: a quote tape that goes silent at the level holds; only the wind-down or the unpriced exit covers it.

### Confirmation

ADR 0119 stays `proposed` until:

* the quote path is tested first on shaped tapes: one quote under the level holds, two spaced quotes under it
  sell, a quote above between them holds, one-sided and crossed quotes neither count nor clear, and the
  equal-stamp and late-quote rules hold;
* the span has a value from the measure, or the lead records that the count stays unbounded;
* the protection part's seam exists and the Mover and the protection part produce the same held trigger and sale on the
  same tape;
* the paper measure has counted the arms.

### Evidence

Read 2026-11-13. The pins are the local read-only copies from `scripts/canon src`,
resolved from `canon/upstreams.lock.json`: `nautilus-trader@d53849b216e2` (v2.0.0rc8) and
`nautilus-trader-v1@27a8e54e7ac3` (v1.231.0).

* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/execution/src/order_emulator/emulator.rs
  — the emulator accepts only `Default`, `BidAsk` and `LastPrice` and cancels any other trigger (`:598-606`); a
  quote tick sets one bid and ask and iterates the orders (`:1076-1091`); release and submission
  (`:1267-1293`, `:1315-1416`) — `resolved`; re-read in the pin above.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/execution/src/matching.rs
  — a stop matches one price, with no count and no span (`:555-580`, `:583-599`) — `resolved`; re-read.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/model/src/enums.rs
  — `DoubleLast` and `DoubleBidAsk` exist as values (`:2221-2223`) and are carried only by the Interactive
  Brokers adapter — `resolved`; re-read.
* https://github.com/nautechsystems/nautilus_trader/blob/27a8e54e7ac3c57d6cbf8891f0283dfbaee97317/crates/execution/src/order_emulator/emulator.rs
  — the same three accepted triggers and the same cancellation of any other (`:570-578`) — `resolved`; re-read.
  `.../crates/model/src/enums.rs:2019-2021` — the same two double values; `.../crates/execution/src/matching_engine/engine.rs:4138`
  names them in a comment only — `resolved`.
* `docs/adr/0119` — the count and the gap on trade prints only, and the refused `BID_ASK` and `trail_mode` with
  persistence (`:59-60`, `:70`, `:103-105`); read in the tree.
* `docs/handbook/practices.md:42` — "A breach needs a count of observations over a span, never one quote, print
  or bar"; read in the tree.
* `src/canon_trading/mover.py` — the four refusals (`:1137-1158`), the one-quote submit (`:4318-4330`), the
  count, gap and trigger defaults (`:1013-1015`, `:1031`), `persistence` (`:1237-1239`), `two_sided`
  (`:1712-1720`), `stamp_of` (`:1869-1876`); read in the tree at `e782817a`.
* `src/canon_trading/session_window.py` — `TRAIL_ARMS` and `DEFAULT_ARM` (`:540-550`), the window trigger
  (`:761`), `size_from_budget=True` (`:1742`); read in the tree at `e782817a`.
* `docs/adr/0146` — the protection part's gap and its carrier (`:8`, `:70-78`); read in the tree.
* Paper account B: 31 exits by the trailing stop in 34 windows, 8 of them within 60 s of the first fill — the
  lead's read of the by-hand window records, 2026-11-13 15:10, kept outside the repository, so `UNVERIFIED`
  for a later reader.
* On the owner's protect practice: per-symbol broker stops elected on one observation sold the whole holding
  within five minutes of the buy and within two minutes of resting — the lead's read, kept outside the
  repository, so `UNVERIFIED` for a later reader.
* A code reading of 2026-11-13, its first finding: the default arm electing on one quote, and the pinned citations above. Its
  second and third findings are related findings handled elsewhere.
