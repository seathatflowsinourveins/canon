---
status: proposed
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation (ADR 0074); the lead reads each paper window
review-by: 2026-12-11
---

# The mover window: one name chosen once and published as the ranking, an entry of at most 500 dollars under a 5,000-dollar engine cap, and each paper run; a bridge until the ranking actor exists

## Context and Problem Statement

ADR 0088's strategy needs a node to run in, a ranking on the node's bus and the rows of a run. The
ranking actor is not built (ADR 0087, numbered and not yet on `main`), and by the lead's decision of
2026-11-11 paper starts as soon as the code exists: stage one is one slot and one trade, on paper, in
the regular session initially. The lead's later decision of 2026-11-11 lifts that session limit:
`--mover` runs the strategy in every extended part too. What stands in for the ranking, and which rows
does a window state?

## Decision Drivers

* Paper is the proving ground and waits for nothing but missing code (the owner's requirement of
  2026-11-11); a window still starts flat, ends flat or fails, and touches no order but its own.
* The strategy reads the ranking from the bus only (ADR 0088), so the stand-in is a publisher, not a
  second way in.
* No cap refuses a sale (ADR 0088), so the engine's per-order cap cannot sit at the entry's own size.

## Considered Options

1. Wait for the ranking actor.
2. Publish the window's one choice, made before the node is built, as the ranking.
3. Let the strategy read the vendor's movers list.

## Decision Outcome

Proposed: **option 2**, in `canon_trading.mover_window` and `canon_trading.session_window`
(`--strategy Mover --mover`). Option 3 is refused by ADR 0088 (the strategy reads no vendor's list;
Alpaca's gainers stay the choice's fallback only); option 1 holds paper back for code that the strategy
does not need in order to be tested.

* **The ranking of a window** is one name: the window's choice (`session_window.choose_mover`, ADR
  0083's dated line: a three-minute sample of the whole market's bars on the part's feed ranked by the
  movers score, or Alpaca's gainers ranked the same way when the sample fails inside the regular
  session; never a warrant, a unit or a right, a
  name with a corporate action since the last session through the trade date, one with no fresh trade,
  or one whose entry is over the strategy's bound (500 dollars for `Mover`, 1,000 for the bridge).
  A `Mover` candidate needs a positive ask, and an overnight candidate must be eligible and not halted
  there; one that fails is passed over by name before the next is tried. `FixedRank`, a `DataActor`, publishes it on `movers-rank` every 15 seconds with the
  time the choice was made as the signal's `ts_event`, since a signal is not replayed to a later
  subscriber. The strategy's staleness row is set to reach from the choice to the window's end, so in
  a window it does not bind. The actor of ADR 0087 replaces `choose_mover` and `FixedRank` together.
* **The rows of a window**, each UNCALIBRATED: the largest entry is 500 dollars (`max_notional`, the
  lead's decision of 2026-11-11), and in shares what 500 dollars buys at the entry limit, the ask of
  the latest quote on the part's feed when the window starts, which is also the position cap (a name whose one share
  is over 500 dollars there is refused); one slot, one name watched, one entry a window; the no-chase cap at 100 times
  the last close, so that it does not bind; a re-price by cancel and a new limit; bars of one minute
  built from the trade stream. The other rows are `MoverConfig`'s defaults (ADR 0088).
* **The engine's per-order cap is an outer bound**, 5,000 dollars, ten times the largest entry. At the
  pin the risk engine holds an equity sale to that cap as it does a buy, so a cap at the entry's size
  denies the sale of a position that rose (ADR 0088). It is there to stop a runaway order. The limit
  that leaves: a position worth ten times its entry is not sold in one order; its sale is sent again
  every `chase_secs`, and the window's account check closes what is left.
* **Paper sizing, superseding the two figures above for paper.** On the owner's decision, 2026-11-11, as the
  lead (canon-fa) recorded it under ADR 0074 rule 1, with the lead's figures of the same evening: the paper
  accounts carry no risk limit. The entry size is the smaller of the account's buying power and its cash at
  the node's start, divided by the slots in use, read again at each start. Cash keeps a margin account's
  buying power, up to 2x overnight and 4x intraday at Alpaca, from sizing an entry above 1x until the
  margin-account build lands (a Codex build the coordinator started on 2026-11-11). The engine's per-order
  notional cap is the entry times the existing outer multiple (`mover_window.OUTER_BOUND_MULTIPLE`, 10), so
  the sale of a position that rose is never denied (ADR 0088). The slots are 7 in the regular session and 7
  after hours (the lead, 2026-11-11), raised only after two paper firings show room, and 7 in pre-market and
  overnight too, by the same worst-minute count (the lead, 2026-11-11); going above that count needs two
  records that agree. What the rule does to `max_shares` and the position cap is settled when it is built. One
  limit: a paper fill takes any size at the quote (Alpaca's paper page: "Your order quantity is not checked
  against the NBBO quantities"), so a size far beyond a name's traded volume teaches less than it seems to.
  The comparison of fills against a replay is where that shows. It is a learning note, not a cap. Alpaca's
  paper page says eligible orders "receive partial fills for a random size 10% of the time", and that paper
  trading does not account for the "market impact of your orders" or "price slippage due to latency"
  (https://docs.alpaca.markets/us/docs/paper-trading.md, read 2026-11-11). `FixedRiskSizer` is not used for
  this rule, since it needs a risk fraction and a stop distance. Live is untouched and stays the owner's. The
  500 and 5,000 dollars above stand as the first windows' record. Not built yet: on 2026-11-11 the window
  sizes by `FixedRiskSizer` at `risk` 0.01 (`mover.py`) under a 500-dollar entry bound
  (`mover_window.ENTRY_NOTIONAL_USD`, applied as `max_shares`) and a 5,000-dollar engine cap.
* **The request budget**: a window whose node sent 180 or more requests to the trading API in any 60
  seconds, or met a 429, fails by name; the strategy's own soft line (140) stops entries first.
* **Sessions**, by the lead's later decision of 2026-11-11: `--mover` runs `Mover` in every extended
  part. `session_window.MOVER_PARTS` gives each part its feed and `slots` row, one by default,
  UNCALIBRATED; the row is forwarded to `MoverConfig`, with the same number watched, rather than
  enforced as a literal in the strategy. The fixed ranking still carries one name. Pre-market and
  after-hours use `sip`; overnight uses `boats` for the sample, current trades and quotes, and minute
  bars for its same-clock normals. The last completed consolidated regular close remains the daily
  bar in a `sip` snapshot (`MoverHistory` routes only the normals to the part's feed). Overnight's
  trade date is the following day from 20:00 New York, including Sunday before a Monday trading day;
  the calendar and `overnight_tradable`/`overnight_halted` checks still apply. The regular part keeps
  the strategy its arguments name and its managed market exit. In every extended part each venue
  order is a marked DAY limit: entry, the emulator's released trailing sell, chase and wind-down;
  the stop cancels, with no market exit. The window's start check, cap and account check after the
  stop stay the window's own (ADR 0044, ADR 0060).
  The same-clock normal windows use the preceding evening of each prior weekday trade date from
  20:00 ET, including open Sunday evenings and excluding closed Friday evenings; after midnight the
  date itself is used. Empty holiday windows are still skipped under the existing lookback bound.
* **The bridge fallback** is explicit: a part's `bridge_reason` runs `SessionMaCross` on the same
  chosen name and feed in its marked limit form. The record names the part, `SessionMaCross` and the
  reason under `mover.fallback`. No shipped part currently needs it. A missing mover data row, an
  unqualified choice or a failed feed refuses by name: changing strategies supplies no missing
  market data. The screener fallback ranks only the regular session, so it remains unavailable
  outside it even though the live sample may run there.
* **The schedule** (the lead's decision of 2026-11-11): the scheduled agent's arguments name the
  strategy of the regular session, `--regular-strategy Mover` beside `--session auto --mover`, and
  none for any other part: `--mover` now chooses `Mover` in those parts too, subject to the explicit
  bridge row above (ADR 0050, ADR 0083). It runs
  on the second paper account (ADR 0069), where this strategy had not run when this was written.

**The replay before paper** (`tests/test_mover_day.py`, `tests/test_paper_mover_day.py`) runs the
strategy on one recorded day with a window's notional, move and entry-count rows: minute bars on SIP,
the feed a node's bars are built
from, and quotes on IEX, the one feed the adapter's quote request serves (ADR 0066). Its data limit:
IEX's top of book is not the national best bid and offer, which on 2026-11-11 decided the result, below.

**Update, 2026-11-11:** the replay's `stage_one` added the check across remade sells that Muse's sixth finding called
for, comparing recorded triggers within the same holding and resetting after flat.
The evidence is
`tests/test_mover_day.py::test_stage_one_checks_a_remade_sells_trigger_until_the_position_is_flat`:
the lower remake failed before the fix; lower, equal, higher and new-entry cases pass after it.

**Follow-up, 2026-11-11 (Antigravity findings 1 to 4):** the original check used the released limit's
new initialization timestamp and sampled after each step. It now uses `init_event.ts_init`, which the
pinned emulator preserves when transforming an order, and reads the cached order's initialization and
update events instead of timer samples. The real-engine tests in `tests/test_mover_day.py` cover an
immediate-fill release, two holdings, a lower remake after a partial entry fill, a sub-second trigger
peak before a remake, and a seeded stop released during submit; the offline checks live there, outside
the paper module. The replay still covers one instrument. It explicitly passes `max_shares=None`,
overridable through `replay`'s changes, and adds no `PositionCap`: unlike the window it has no starting
entry ask from which to derive the share bound. That sizing difference remains an open question; no
replacement cap policy is chosen here.

### Consequences

* Good: the strategy runs on paper today through the same signal the ranking actor will publish.
* Bad: one name, chosen once: a window cannot rotate, and a name that stops moving ends the window's
  chances.
* Bad: the replay cannot exercise a fill on a name IEX quotes widely; only paper does.

### Confirmation

* `tests/test_mover_window.py` (each extended part builds `Mover` with one configurable slot),
  `tests/test_mover.py` (the window's parts on trades and quotes, the outer bound, marked DAY limit
  entry and released sale for each extended part) and
  `tests/test_session_window.py` (the node's build, the choice and the end handed to it, the request
  ceiling, `--mover` required, selection in every part, the named bridge fallback, overnight feed
  routing and Sunday's next trade date). These are offline builds and simulations; each extended
  part on account B remains UNVERIFIED until a Claude lane runs it on paper.
* The replay, 2026-11-11, `mise run paper:test -- -s -k test_paper_mover_day` on paper account A, data
  reads only. On IEX bars alone no name had enough bars (NXXT 43, NCT 61, SCKT 99 against a floor of
  120), so the bars moved to SIP with the floor unchanged. First SIP run: NXXT (270 bars) and NCT (134)
  passed over for quote gaps of five minutes or more, read as halts; SCKT (121 bars, 2,592 IEX quotes,
  prior close 0.2511 with no corporate action) replayed, and showed two defects: the entry kept
  filling after its sale had released, and it was sent at an ask of 0.3857 against a bid near 0.28.
  Both were fixed with failing tests first (ADR 0088). Rerun, exit 0: seven rising runs, each refused
  because IEX's bid was under the planned stop; no entry, no fill.
* **Window 1**, 2026-11-11, 13:20:49 to 14:04:06 New York, paper account A, the regular session; from
  an earlier form of the strategy's first commit (`660b37e`, rewritten since and not on `main`) with
  uncommitted changes, before the fixes it led to. The name was CENN, from the stream sample: up 128.3% from a close of 0.1227, a
  dollar-volume ratio of 1,031; GOVX and SLXN ranked above it and were passed over, for a reverse
  split and for no fresh trade. No rising run of three closes came for 37 minutes. At 13:58:00 a DAY
  limit to buy 1,686 at 0.2141 reached the node as accepted 44 ms after its submit and filled in six parts in ten seconds,
  358.24 dollars. The emulated trailing stop-limit was released at 13:59:05 at a bid of 0.2085, as a
  limit whose acceptance came back in 23 ms; 1,044 filled at 0.2084, and the 642 left were re-priced by a cancel
  and a new limit and filled by 13:59:16. Result: 349.12 dollars back, 9.11 lost, 2.54% under holding
  flat. The window ended `ok` and flat without its account check closing anything: 307 trading-API
  requests, at most 15 in 60 seconds, no 429. What it showed, each fixed since (ADR 0088): every
  further fill of the entry remade the trailing sell a second later and from a lower trigger (0.2058
  down to 0.2039); the log said nothing of the 37 minutes; the record's notes were empty. Its record
  is `window-20261111T182049Z.json`, kept outside the repository.
* **Window 2**, 2026-11-11, from 14:13:46 New York, account A, the regular session; from the commit
  that remakes the trailing sell in the same step, before this change's last fixes. The name was ELOG,
  from the stream sample, nothing passed over: up 84.1% from a close of 0.5922 to 1.09, a dollar-volume
  ratio of 10,058; the entry limit at the start was 1.10, so at most 454 shares. It then fell under a
  dollar. Four minutes at an unchanged 0.9100 restarted the run each time; closes of 0.7540, 0.7690,
  0.7833 and 0.8292 qualified. At 14:26:00.30 a DAY limit to buy 454 at 0.8318, on the four-decimal
  band, reached the node as accepted 30 ms later and filled whole at 14:26:02.12, 377.64 dollars. The
  trailing sell was made 0.18 s after the fill, its trigger 0.8099. At 14:26:07.19 the emulator lifted
  the trigger to 0.8340, which a bid of 0.8507 gives (the quote is not in the log), and released
  within the same millisecond at a bid of 0.8258; the limit's acceptance came back in 25 ms and it
  filled in two parts at 0.8258 by 14:26:10.40, 374.91 dollars. Result: 2.72 lost, 0.72%, 8 seconds
  held. The window's one entry was used. The machine then slept from 14:40 to 15:49, and the window
  waits for its end on the event loop's monotonic clock (`session_window._run_until`), which a sleep
  stops, so it ran past its planned end of 14:56. It was ended by an interrupt at 15:53:38, seven
  minutes before the close: flat, nothing for its account check to cancel or close, 228 trading-API
  requests, at most 14 in 60 seconds, no 429. Its record reads `failed: KeyboardInterrupt`, so it
  does not count as a window that ended `ok`; the wait is every window strategy's, and is not fixed
  here. Its record is `window-20261111T191346Z.json`.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records.

* NautilusTrader at the pin
  (https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186) —
  `python/nautilus_trader/common/__init__.pyi:688`, `:720`, `DataActor` and its `publish_signal(name,
  value, ts_event)`; `python/nautilus_trader/live/__init__.pyi:1126`, `LiveNode.add_actor`;
  `crates/risk/src/engine/mod.rs:2491-2507`, the per-order notional cap, whose one exemption
  (`:706-720`, a full exit of a crypto derivative) no equity order meets. Resolved.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca — extended eligibility requires the boolean
  `extended_hours` mark and a limit with DAY or GTC time in force; pre-market 04:00–09:30 and
  after-hours 16:00–20:00 New York. https://docs.alpaca.markets/us/docs/245-trading-for-trading-api —
  overnight 20:00–04:00 before the trade date, following the NYSE calendar; limits only, DAY or GTC;
  BOATS supports quotes, trades, snapshots and historical bars for the account's plan. Read 2026-11-11
  from the HTML pages linked by https://docs.alpaca.markets/llms.txt; paper support for the new mover
  windows is UNVERIFIED here.
* alpaca-py at the pin
  (https://github.com/alpacahq/alpaca-py/tree/c42add7c0a7026e2ead3ed64e76f4def2b99fda4) — `alpaca/data/live/stock.py`, the constructor's
  `feed` and BOATS stream endpoint; `alpaca/data/requests.py`, `StockBarsRequest.feed`,
  `StockSnapshotRequest.feed` and `StockLatestQuoteRequest`; `alpaca/trading/requests.py`,
  `OrderRequest.extended_hours` and `LimitOrderRequest`. NautilusTrader at the pin,
  `python/nautilus_trader/common/__init__.pyi`, `OrderFactory.limit` and `trailing_stop_limit`, and
  `crates/execution/src/order_emulator/emulator.rs`, `submit_released_order`, which retains submit
  params when transforming the order. Read locally 2026-11-11; resolved.
* That a signal is not replayed to a later subscriber: the ranking lane's finding at the pin on
  2026-11-11, not reproduced here. UNVERIFIED in this record; the actor repeats its ranking either way.
* https://alpaca.markets/support/usage-limit-api-calls — "currently 200 requests per minute, per
  account", the figure the ceiling sits under. Fetched by the evidence-verifier, 2026-11-11.
* `src/canon_trading/mover_window.py`, `src/canon_trading/session_window.py` and their tests — as
  described, read in this tree.
* The three replay runs and each window's record and node log (`window-<start>.json`, `.log`), read
  here on the day; all kept outside the repository, so UNVERIFIED for a later reader.
* An `evidence-verifier` pass, 2026-11-11: it corrected the two acceptance times (now both the
  node's receive time), the record that refuses option 3, the price the shares are counted at, the
  choice's bound, window 1's commit, and this section's labels.
* The owner's requirement of 2026-11-11 and the lead's decisions of that day (500 dollars, the outer
  bound, paper before the ranking actor) — instructions, not evidence: each number is UNCALIBRATED.
* Antigravity's read of the extended-part change, 2026-11-11: the pre-midnight action end date, the
  candidate's affordable ask, candidate-level overnight eligibility and Sunday-evening normals were
  reproduced by failing tests before their fixes in `tests/test_session_window.py`; inherited comments
  in ADR 0083 and the scheduler were corrected. The final quote and eligibility rechecks remain in
  place. All of this evidence is offline; each extended-part firing on account B is still UNVERIFIED.
* evidence-verifier on the sizing edits, 2026-11-11: the cap at the entry contradicted this record's
  driver and ADR 0088, and the window sizes by `FixedRiskSizer` under the 500. Corrected above, with the
  lead's figures of the same evening.
