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
the regular session. What stands in for the ranking, and which rows does a window state?

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
(`--strategy Mover --mover`). Option 3 is refused by ADR 0083; option 1 holds paper back for code that
the strategy does not need in order to be tested.

* **The ranking of a window** is one name: the window's choice (`session_window.choose_mover`, ADR
  0083's dated line: a three-minute sample of the whole market's sip bars ranked by the movers score, or
  Alpaca's gainers ranked the same way when the sample fails; never a warrant, a unit or a right, a
  name with a corporate action since the last session, one with no fresh trade, or one whose share is
  over the cap). `FixedRank`, a `DataActor`, publishes it on `movers-rank` every 15 seconds with the
  time the choice was made as the signal's `ts_event`, since a signal is not replayed to a later
  subscriber. The strategy's staleness row is set to reach from the choice to the window's end, so in
  a window it does not bind. The actor of ADR 0087 replaces `choose_mover` and `FixedRank` together.
* **The rows of a window**, each UNCALIBRATED: the largest entry is 500 dollars (`max_notional`, the
  lead's decision of 2026-11-11), and in shares what 500 dollars bought at the choice's price, which is
  also the position cap; one slot, one name watched, one entry a window; the no-chase cap at 100 times
  the last close, so that it does not bind; a re-price by cancel and a new limit; bars of one minute
  built from the trade stream. The other rows are `MoverConfig`'s defaults (ADR 0088).
* **The engine's per-order cap is an outer bound**, 5,000 dollars, ten times the largest entry. At the
  pin the risk engine holds an equity sale to that cap as it does a buy, so a cap at the entry's size
  denies the sale of a position that rose (ADR 0088). It is there to stop a runaway order. The limit
  that leaves: a position worth ten times its entry is not sold in one order; its sale is sent again
  every `chase_secs`, and the window's account check closes what is left.
* **The request budget**: a window whose node sent 180 or more requests to the trading API in any 60
  seconds, or met a 429, fails by name; the strategy's own soft line (140) stops entries first.
* **Sessions**: the regular session only, until a marked limit of this strategy has run outside it. In
  the session the wind-down and the stop are upstream's market exit; the window's start check, cap and
  account check after the stop are the window's own (ADR 0044, ADR 0060).

**The replay before paper** (`tests/test_mover_day.py`, `tests/test_paper_mover_day.py`) runs the
strategy on one recorded day with a window's rows: minute bars on SIP, the feed a node's bars are built
from, and quotes on IEX, the one feed the adapter's quote request serves (ADR 0066). Its limit: IEX's
top of book is not the national best bid and offer. On 2026-11-11 that decided the result, below.

### Consequences

* Good: the strategy runs on paper today through the same signal the ranking actor will publish.
* Bad: one name, chosen once: a window cannot rotate, and a name that stops moving ends the window's
  chances.
* Bad: the replay cannot exercise a fill on a name IEX quotes widely; only paper does.

### Confirmation

* `tests/test_mover.py` (the window's parts on trades and quotes, the outer bound) and
  `tests/test_session_window.py` (the node's build, the choice and the end handed to it, the request
  ceiling, `--mover` required).
* The replay, 2026-11-11, `mise run paper:test -- -s -k test_paper_mover_day` on paper account A, data
  reads only. On IEX bars alone no name had enough bars (NXXT 43, NCT 61, SCKT 99 against a floor of
  120), so the bars moved to SIP with the floor unchanged. First SIP run: NXXT (270 bars) and NCT (134)
  passed over for quote gaps of five minutes or more, read as halts; SCKT (121 bars, 2,592 IEX quotes,
  prior close 0.2511 with no corporate action) replayed, and showed two defects: the entry kept
  filling after its sale had released, and it was sent at an ask of 0.3857 against a bid near 0.28.
  Both were fixed with failing tests first (ADR 0088). Rerun, exit 0: seven rising runs, each refused
  because IEX's bid was under the planned stop; no entry, no fill.
* **Window 1**, 2026-11-11, 13:20:49 to 14:04:06 New York, paper account A, the regular session; from
  the strategy's first commit with uncommitted changes (their diff's SHA-256 begins `9b7991ed`), before
  the fixes it led to. The name was CENN, from the stream sample: up 128.3% from a close of 0.1227, a
  dollar-volume ratio of 1,031; GOVX and SLXN ranked above it and were passed over, for a reverse
  split and for no fresh trade. No rising run of three closes came for 37 minutes. At 13:58:00 a DAY
  limit to buy 1,686 at the ask, 0.2141, was accepted in 20 ms and filled in six parts in ten seconds,
  358.24 dollars. The emulated trailing stop-limit was released at 13:59:05 at a bid of 0.2085, as a
  limit Alpaca accepted in 23 ms; 1,044 filled at 0.2084, and the 642 left were re-priced by a cancel
  and a new limit and filled by 13:59:16. Result: 349.12 dollars back, 9.11 lost, 2.54% under holding
  flat. The window ended `ok` and flat without its account check closing anything: 307 trading-API
  requests, at most 15 in 60 seconds, no 429. What it showed, each fixed since (ADR 0088): every
  further fill of the entry remade the trailing sell a second later and from a lower trigger (0.2058
  down to 0.2039); the log said nothing of the 37 minutes; the record's notes were empty. Its record
  is `window-20261111T182049Z.json`, kept outside the repository.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records.

* NautilusTrader at the pin
  (https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186) —
  `python/nautilus_trader/common/__init__.pyi:688`, `:720`, `DataActor` and its `publish_signal(name,
  value, ts_event)`; `python/nautilus_trader/live/__init__.pyi:1126`, `LiveNode.add_actor`;
  `crates/risk/src/engine/mod.rs:2491-2507`, the per-order notional cap. Resolved.
* That a signal is not replayed to a later subscriber: the ranking lane's finding at the pin on
  2026-11-11, not reproduced here. UNVERIFIED in this record; the actor repeats its ranking either way.
* The 200 requests a minute an account that the ceiling sits under is ADR 0083's figure, taken from
  that record and not read again here. UNVERIFIED in this record.
* `src/canon_trading/mover_window.py`, `src/canon_trading/session_window.py` and their tests — as
  described; the replay's figures are from the two runs named in Confirmation, whose full output is
  kept outside the repository.
* The owner's requirement of 2026-11-11 and the lead's decisions of that day (500 dollars, the outer
  bound, paper before the ranking actor) — instructions, not evidence: each number is UNCALIBRATED.
