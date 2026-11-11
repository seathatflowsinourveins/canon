---
status: proposed
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation (ADR 0074); the lead reads it after its first paper window
review-by: 2026-12-11
---

# The mover strategy class: ranked names in slots, a limit entry on the first rising run, the engine's emulated trailing stop-limit and a cancel-then-place exit; proposed until its paper windows are read

## Context and Problem Statement

ADR 0083's gap 3: no upstream ships a strategy of this kind, so the class is the one piece written here,
as a composition of upstream parts. The owner's requirement of 2026-11-11 is the four moves over several
names: rank, enter early, protect, re-enter or rotate, long only, on paper in every session. Which upstream
part carries each step, and what is ours? ADR 0083 asked for this record before any code; by the lead's
decision of 2026-11-11 the record and the code are one change. The window that runs the class, its rows
and its paper runs are ADR 0091. It recommends no trade.

## Decision Drivers

* Upstream parts as shipped; what has none is named and kept small (rule 3, ADR 0083).
* The owner's requirement of 2026-11-11: every number is a registered row, UNCALIBRATED until measured;
  long only; no cap, budget, ranking or control refuses or delays a sale, and entries yield.
* A share is never without its sale and never has two.

## Considered Options

1. One strategy instance a name, created and removed at run time by upstream's `Controller`.
2. One strategy that manages slots, over instruments loaded when the node is built.
3. Upstream's example strategies, configured.

## Decision Outcome

Proposed: **option 2**, `canon_trading.mover.Mover`. Option 3 has no strategy of this kind (ADR 0083).
Option 1 is upstream's mechanism for a universe that changes and is UNVERIFIED in a live node. This
departs from ADR 0083, whose gap 2 leans to option 1 and whose M2 ties re-entry and rotation to a paper
run of `Controller`: here they are plain state of one strategy, built and switched off in stage one (one
slot, one entry a run). The universe is the instruments the node loaded, today one name a window, so
loading every tradable instrument at build is not built, and the node's cap map is still fixed at build.

**The parts** (`nt:` is `nautilus-trader@d53849b216e2`, v2.0.0rc8):

| Step | Upstream part | Where |
|---|---|---|
| The ranking | `Strategy.subscribe_signal`, `on_signal`; the value is a string | `nt:python/nautilus_trader/trading/__init__.pyi:696`, `:633`; `docs/concepts/message_bus.md:243-264` |
| Entry | `OrderFactory.limit`, DAY, at the ask | `nt:python/nautilus_trader/common/__init__.pyi:1388` |
| Size | `FixedRiskSizer.calculate(entry, stop_loss, equity, risk, hard_limit=…)` | `nt:python/nautilus_trader/risk/__init__.pyi:16-29`; `crates/risk/src/sizing.rs:39-117` |
| Protection | The order emulator holding `OrderFactory.trailing_stop_limit`, trigger `BID_ASK` or `LAST_PRICE`, released as a `LIMIT` | `nt:…/common/__init__.pyi:1523-1547`; `docs/concepts/orders/emulated.md:15-19`, `:129` |
| The mark outside the session | A release keeps the submit's params | `nt:crates/execution/src/order_emulator/emulator.rs:1368`, `:1522-1531`; its test, `:4513`, covers stop orders |
| A re-price | `cancel_order`, then a new `OrderFactory.limit` | `nt:python/nautilus_trader/trading/__init__.pyi:571` |
| Prices | `TieredTickScheme.next_ask_price`, `next_bid_price` | `nt:crates/model/src/instruments/tick_scheme.rs:525`, `:555` |
| The end in the session; timers | `market_exit()`; `Clock.set_timer`, `set_time_alert` | `nt:docs/concepts/strategies.md:708-728`, `:304-342` |

**What is ours**, stated in the module header: reading a rising run from bars; the slots; the re-entry
and rotation rules; the chase's price and floor; the yield of entries to the request budget
(`canon_alpaca.providers.request_counts`). The order path's rules, each with a test:

* No order is modified (the lead's decision of 2026-11-11, until the adapter's replace chain has passed
  two clean outside reviews). A re-price is cancel, the cancel seen terminal, then a new limit; the
  emulated trailing sell is canceled and made again when the held quantity changes, from the trigger
  it had reached. The cost is two requests and a moment with no working sale.
* At the release a working entry is canceled. A sale that covers less than is held is canceled and the
  next covers all of it.
* The chase floor belongs to the position: `chase_cap` under its first sale price, kept when that sale
  is refused.
* An entry whose bid is already under its planned stop is refused by name: the quote is wider than the
  plan the size rests on.
* Every limit price the strategy computes is a tick of upstream's tiered scheme stating Alpaca's two
  bands. The emulator prices a release itself, on the instrument's 0.0001 increment
  (`nt:crates/execution/src/trailing.rs:182`), so the trailing sell's limit offset is a whole number of
  cents, which is a price Alpaca takes under a bid on either side of a dollar; for a name under a
  dollar the release sits up to a cent lower than `exit_through` asks, and the size takes that in.
* A live node builds a bar for every clock minute, a paused one too (`time_bars_build_with_no_updates`,
  `nt:crates/data/src/engine/config.rs:36-38`). A repeated close is not a rise, so a pause restarts the
  run; the default is left on.

**The ranking's contract** with the ranking actor (ADR 0087, numbered and not yet on `main`): the signal
`movers-rank`, its value the ranked instrument IDs best first, at most 20, each with its move from the
last regular close (`ID=move;ID=move`), its `ts_event` the time the ranking was computed. A ranking older
than `rank_max_age_secs`, or one that does not parse, means no entry. The strategy reads no vendor's list.

**The rows** are `MoverConfig`'s defaults, all UNCALIBRATED: `slots` 1, `watch` 3, `risk` 0.01 of the
balance, `run_bars` 3, `entry_ticks` 0, `exit_ticks` 1, `trail` 0.02 of the entry price (a fixed offset
under the highest bid), `exit_through` 0.005, `chase_cap` 0.02, `chase_secs` 5, `chase` `resend`,
`flow_share` 0.05 of `flow_bars` 5, `entry_timeout_secs` 30, `cooldown_secs` 300, `rank_max_age_secs` 90,
`soft_line` 140 requests in 60 s, `trigger` `BID_ASK`; `max_shares` and `entries` unbounded;
`max_notional` (the largest entry) and `max_move` (the no-chase cap) have none and are stated by each
run. A **hold-to-close** row is the same entry and size with `protect=False`: no trailing sell, sold at
the wind-down; a study's baseline, since a trial that never trades stops a ranking (ADR 0010).

**Departures from the requirement, each not built:** a re-priced entry; the gain lock; a breach that must
persist (in the replays here the emulator released on the first quote at the trigger); a broker-held
stop for an outage; slots reserved for the newest movers; the reclaim as a pullback, a held higher low
and a hold (here: a rising run that closes above the sale's price, after the cooldown); no order into a
halt (ADR 0083, gap 4); each exit's counterfactuals (the strategy keeps every fill with its time; a
replay computes them).

### Consequences

* Good: one class from one slot to several; each order path is an upstream call.
* Bad: a gap through both the trigger and the limit leaves a resting sale, chased only to its floor.
* Bad: a partly filled entry is protected from the next step, a second later, not at once.
* Bad: nothing bounds the time without a working sale when a replacement is refused again and again;
  `is_closed` after a reconciliation cancel is not proof of a venue cancel (ADR 0075); the market exit
  at the wind-down does not wait for cancels to be terminal; with the `LAST_PRICE` trigger a sub-penny
  print at or above a dollar would still release a sub-penny limit (a window uses `BID_ASK`).

### Confirmation

* `tests/test_mover.py`: 71 tests, the order-path cases through a real `BacktestEngine` on generated
  bars, trades and quotes. `tests/test_mover_day.py`: the replay and its shape checks on a generated tape.
* UNVERIFIED at rc8, from rc9's release notes: `market_exit` "skipping emulated and active-local orders
  during cancellation" (a test here shows the trailing sell canceled no later than the exit's order);
  `FixedRiskSizer` "exceeding risk and quantity limits"; `Portfolio` reservations "remaining locked after
  rejected LIT and trailing stop limit orders", which sizing reads through the free balance.
* On paper: ADR 0091 holds each window. UNVERIFIED until one shows it: the emulated trailing stop-limit
  on Alpaca's paper venue, the mark travelling on a release, and the size under paper's fills.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records.

* NautilusTrader at the pin
  (https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186) —
  every `nt:` line above; `crates/risk/src/engine/mod.rs:2491-2507`, the per-order notional cap, which
  holds for an equity order whatever its side (its one exemption, `:667-720`, is a full exit of a crypto
  derivative); `crates/trading/src/python/strategy.rs:2016-2037`, `submit_order` borrowing the strategy
  mutably. Resolved.
* That a handler reading the engine during the strategy's own command raises: the lead's finding from a
  paper run on 2026-11-11. UNVERIFIED in this record; the handlers store only what an event carries, and
  their test uses a stand-in.
* NautilusTrader v2.0.0rc9's `RELEASES.md` (a local copy at `da6b57aefdd6`), lines 170, 180 and 194 —
  the three fixes quoted in Confirmation. UNVERIFIED against the published release; the pin is not moved.
* Probes run here on 2026-11-11 with nautilus-trader 2.0.0rc8, kept outside the repository and not
  reproduced by a test, so UNVERIFIED in the repository: on bars alone the emulator cannot activate a
  trailing stop ("no market price available"); with a limit offset under the trailing offset the
  released sell limit rested above a fallen market; with bars and quotes in one replay the simulated
  venue filled a resting sell limit at 10.99 from the bar's earlier path while the bid was 10.84, so
  replays turn bar execution off.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "Only limit orders with `time_in_force` set
  to `day` or `gtc` orders are accepted as extended hours eligible"; "Limit price >=$1.00: Max Decimals=
  2", "Limit price <$1.00: Max Decimals = 4"; "Trailing stop will not trigger outside of the regular
  market hours", which is Alpaca's own trailing stop, not the emulated one here. Fetched.
* A second reader (Codex) on the strategy's first commit, 2026-11-11: five order-path findings, each
  reproduced by a failing test before its fix; `/code-review` at high effort, ten findings, each fixed
  the same way. An `evidence-verifier` pass the same day corrected line ranges, the cap's exemption
  and the labels above.
* The owner's requirement of 2026-11-11 — an instruction, not evidence: each number is UNCALIBRATED.
