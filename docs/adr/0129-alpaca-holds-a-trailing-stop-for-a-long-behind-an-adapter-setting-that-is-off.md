---
status: proposed
date: 2026-11-12
decision-makers: lead orchestrator (Claude) for the unit, its conditions and where it may be used; repo owner ratifies
review-by: 2026-12-12
---

# Alpaca holds a trailing stop for a long, sent by the adapter behind a setting that is off; read on paper: a replace under a named ID and an election on the stop's own order; open: Alpaca's `replaced` update for a trailing stop, a distance under its smallest trail, an oversell where shorting is allowed, an election after a replace, and a `GTC` stop at the next open

## Context and Problem Statement

A stop the strategy holds (ADR 0088, ADR 0119) or NautilusTrader's emulator holds protects a long only
while the node runs. A node that is stopped, or a machine that sleeps, leaves the holding with nothing.
Alpaca has a trailing stop of its own that rests at the venue, and the adapter refused the order type.
What may the adapter send, and where may a node use it before a paper run has shown how Alpaca elects
and fills it?

## Decision Drivers

* Glue only: the adapter sends the order Alpaca documents and builds no trailing logic of its own.
* What Alpaca's page leaves unsaid is not guessed at in code. It is read on paper first.
* A sell must not open a short. The order a triggered trailing stop becomes is a market order, and
  `sell_to_close` on a market order is not shown (ADR 0111's gap).
* With the setting off, nothing changes on the wire or at start-up.

## Considered Options

1. Keep refusing the order type; a stop exists only while a node runs.
2. Send NautilusTrader's `TRAILING_STOP_MARKET` SELL as Alpaca's `trailing_stop`, behind a setting.
3. A plain stop resting at Alpaca, which the adapter already sends, moved by the node.
4. A trailing stop as the stop-loss leg of a bracket or OCO order.

## Decision Outcome

Chosen: **option 2**, `proposed` until the gaps below close. Option 1 leaves the problem. Option 3 rests
at the venue but stands still while the node is down. Option 4 is not offered: trailing stops "are
currently supported only with single orders".

| Part | What it is |
|---|---|
| The setting | `submit_trailing_stops` on `AlpacaExecutionClientConfig`, off. Off, a trailing stop is denied by name and nothing is asked or sent. Two things differ from `main` even so: `sell_to_close_order_types` accepts the trailing stop where it raised, and a modify carrying `trailing_offset` is refused where the parameter was ignored. |
| The order | A whole-share SELL, `DAY` or `GTC`, `DEFAULT` trigger, no activation or trigger price. A `PRICE` offset goes as `trail_price`; `BASIS_POINTS` as `trail_percent`, divided by 100. A BUY and every other form are refused before transport. |
| The session | Alpaca's clock must say the regular session is open, for a submit and for a change of the trail. One answer is reused until the boundary it names, its age the longer of the monotonic clock's and the wall clock's, since the first stops while a machine sleeps. A closed, unreadable or absent clock refuses by name. A quantity-only replace and a cancel need no open session. |
| The election | Alpaca's alone: "elected on the consolidated print", by a trade at or under the stop "provided the electing trade is not outside of the NBBO", and "will not trigger outside of the regular market hours". Then "the order turns into a market order". |
| The trail | Changed through `ModifyOrder` with `params={"trailing_offset": …}`, the convention of upstream's Interactive Brokers adapter, sent as the replace's `trail`. The kind of offset cannot change. |
| A replacement's ID | Every replace the adapter sends, a stop's or not, names a new client order ID: the order's own with a nineteen-digit clock stamp after its count. It is of the form the node writes, so an account read by client order ID shows the replaced stop as the node's (the lead, 2026-11-12), and it is at most 128 characters or not sent. An order whose own ID does not end in a number gets Alpaca's ID as before. A replacement adopted under any ID the adapter did not name (Alpaca's, or a hand's) is named in a warning. Custody counts an order that replaces another by what it filled beyond that one, so a chain is counted once and a foreign order's fills stay foreign (ADR 0042's paper reading); a filled replacement of ours whose replaced order is older than custody's lookback freezes its symbol. NautilusTrader's client order ID does not change, and a report still maps back through `replaces`, never through the ID. |
| The intent | A trailing stop is in `sell_to_close_order_types` only when a node names it there. A reduce-only one is refused otherwise. |
| A start with no cache | An open trailing stop the cache does not hold maps only with `report_external_trailing_stops`; else the start fails by name. Nothing in a client order ID tells one node's order from another's. |

**Where it may be used.** Only on a long-only account, until the oversell reads named under Gaps are in
this record. They were made on 2026-11-13 for the `DAY` form (Gaps); the `GTC` form is not shown, and
the setting stays off. On a long-only account
Alpaca refused a node-sent sell beyond the holding (ADR 0111, a 403); that it refuses the market order a
trigger creates the same way is UNVERIFIED. A protector uses `GTC` for a holding that outlives the
session; a Mover window uses `DAY`. No node on `main` turns the setting on. The change that
first does holds two conditions (the lead, 2026-11-12):

* (a) no window turns the setting on before the proof's reads are in this record;
* (b) if the proof shows a fill under a new venue ID with no `replaces` link, the adapter handles that
  form, test first, before any window uses the venue stop. Today such a fill is counted as an unknown
  leg and the order keeps its own ID; Alpaca documents no such form, so none is built.

**Limits, read and not worked around.** The pin's close only by a pin move in which upstream changes
them, checked then. A strategy's `modify_order` needs a quantity, price or trigger change, so a
trail-only change does not reach the adapter from a strategy; a real quantity change carries it. There
is no `OrderTriggered` for a market-style stop: the election shows as a fill. `OrderUpdated` does not
change a cached order's trailing offset. `OrderStatusReport` has no field for the high-water mark. The
risk engine wants a cached trade for a `DEFAULT` trailing stop, so the node needs a trade feed.
Alpaca's close by no read: a submit that crosses the close in flight is "queued up for release the next
trading day", and outside the regular session nothing here protects a holding (ADR 0105).

**Gaps, and what would close each.** The reads are taken in the regular session on a long-only paper
account unless a gap says otherwise, then one opt-in paper test runs through the adapter.

* The intent on the order a trigger creates: read over REST. (A second sell for shares a resting stop
  holds is read: refused, next line.)
* The long-only bound lifts on evidence only (the lead, 2026-11-12): on a paper account that allows
  shorting, a trailing stop marked `sell_to_close` for more shares than are held, and a second sell for
  shares a resting stop holds. If Alpaca refuses both, a venue stop cannot oversell there. Observed on
  2026-11-13, account A with shorting allowed for each run, by a one-off probe over REST and not through
  the adapter, each for a `DAY` stop: a second sell for shares a resting stop holds, refused with 403
  40310000, marked and unmarked (19:29 UTC, and an earlier run that day); a marked stop for two shares
  with one held, refused with 403 40310000, as was the unmarked one (19:29 UTC); a marked stop with no
  share held, refused with 422 42210000, "position intent mismatch" (19:33 UTC). So the condition stated
  here is met for the `DAY` form on paper (the lead, 2026-11-13). The setting stays off by default and
  nothing turns it on. Still not shown: the `GTC` form of the oversized case and of the case with no
  share held; the intent on the market order a triggered stop becomes; an unmarked trailing stop with no
  share held, which nothing here sends. The runs are in ADR 0111's Confirmation and ADR 0142's Evidence.
* The venue ID and order type a fill arrives under after the election: read on 2026-11-13 for a stop
  never replaced (Confirmation). Still open: the same after a replace, where the fill would arrive on
  the replacement, and a partial election.
* Submit, read back, a change of quantity with the offset, and cancel, through the adapter, and whether
  Alpaca takes the client order ID a replace names: read on 2026-11-13 (Confirmation). Still open:
  whether Alpaca sends its `replaced` update for a trailing stop, which the adapter's `OrderUpdated`
  waits for.
* A `GTC` stop electing at the next open, where Alpaca warns of a worse fill: one held overnight.
* A planned distance under Alpaca's smallest trail (Confirmation; the lead, 2026-11-13): the Mover's
  floor is two quoted spreads, which on a tightly quoted name is under a thousandth of the price, so a
  venue stop at that distance would be refused. The unit that first turns the setting on for a window
  says what happens to such a distance, with a test. Nothing changes until then; the setting is off.

### Consequences

* Good: a long has a stop that outlives the node, for the price of one clock request a session.
* Bad: the election is one print inside the NBBO, with none of the counted, spaced prints ADR 0119
  has a strategy wait for. A node uses one or the other for a holding.
* Bad: Alpaca's page says it becomes a market order; one such fill has been seen on paper, for one
  share (Confirmation).
* Bad: the stop's level between reports is Alpaca's; the node sees it only in a report.

### Confirmation

`packages/canon-alpaca/tests/test_venue_trailing_stops.py`, 66 cases on a real NautilusTrader node
against the tests' stand-in venue; no request reaches Alpaca. That is a simulation, not a paper run.
They cover: the setting off; each refused form; the two offsets on the wire; the clock open, closed,
unreadable, absent, reused to its boundary and not trusted past it after a sleep; the trail change and
the quantity-only replace; a cancel sent during the clock read; fills under the order's own ID and
under one linked by `replaces`; a fill under an unlinked ID, counted and not adopted; and a start with
and without the cache. `packages/canon-alpaca/tests/test_replacement_client_order_id.py` covers the
replacement's ID on the same stand-in, which refuses a named ID an order already carries: its form and
length, two replaces in one reading of the clock, and a restart's reports under the node's own ID;
`tests/test_custody.py` reads such an ID as the node's and a hand's replacement as foreign. Run in
`scripts/ci`.

**Paper reads, 2026-11-13, regular session, one long-only paper account, AAPL**, through the adapter
on a node (`tests/test_paper_venue_trailing_stop.py`, `tests/test_paper_venue_trailing_stop_election.py`,
`packages/canon-alpaca/tests/test_paper_replace.py`; the account flat before and after each run):

* A `DAY` trailing stop of 5 percent for two shares rested as `trailing_stop`, `sell_to_close`, under
  the node's own client order ID. A replace to one share and 4 percent was taken under the named ID
  (the node's with a clock stamp), which reads as the node's lineage, and the cancel ended the
  replacement.
* Alpaca has a smallest `trail_price`, observed and not documented: one cent was refused with 422,
  code 42210000, "trail_price must be >= 0.1% of the stock price". Which price it measures, and
  whether `trail_percent` has the same bound, are UNVERIFIED.
* A stop a thousandth of the share's cost and a cent wide, for one share, was accepted, followed the
  mark, and was elected 41 seconds after it was placed. Alpaca booked the sale on the stop's own
  order: the test's read shows one sell under the stop's client order ID, still type `trailing_stop`,
  `sell_to_close`, no second order, no `replaces` link, no legs; the node's log of the run shows four
  `OrderUpdated` with a rising trigger and the `OrderFilled` under the venue order ID of the
  acceptance. The node's own order got that one fill and its position closed. So condition (b)'s
  form, a fill under a new venue ID, did not occur for a stop never replaced.
* After the replace, `OrderUpdated` reached the node ten seconds later, from the engine's in-flight
  query and not from a `replaced` trade update; the engine's event kept the first venue order ID
  while the adapter already held the replacement. Whether Alpaca sent `replaced` is not in the run's
  log: UNVERIFIED. Until it is read, a tighten is confirmed to the node only by that query.
* With the same account's long-only setting off for one run and restored in it, one share held and
  a `DAY` trailing stop for it marked `sell_to_close` resting: a limit sell of one share was refused
  with 403, code 40310000, "insufficient qty available for order (requested: 1, available: 0)", both
  marked `sell_to_close` and with no position intent. So while a resting stop holds the shares, Alpaca
  let no second sell out for them, shorting allowed or not. That run did not send a trailing stop for
  more shares than are held, since its guard read Alpaca's open-orders listing once and it still showed
  the canceled stop. Two later runs of a one-off probe that day did, over REST and not through the
  adapter, and sent a marked stop with no share held: each was refused (Gaps).
* Not shown: a partial election, an election after a replace, an election with the node down, and
  more than one share or symbol.
* These are the proof's reads condition (a) waits for. What Gaps still lists binds the unit that
  first turns the setting on for a window.

## Evidence

Read 2026-11-12.

* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "Trailing Stop Orders": `trail_price`,
  `trail_percent`, `hwm`, the replace's `trail` ("you cannot change the price trailing to the percent
  trailing or vice versa"), the three notes quoted above, valid time in force "day" and "gtc", and "The
  potential for such vulnerability increases for GTC orders across trading sessions"; "Order Handling
  Standards at Alpaca Securities LLC": "Stop orders and trailing stops are elected on the consolidated
  print… provided the electing trade is not outside of the NBBO"; "Orders Submitted Outside of Eligible
  Trading Hours": "queued up for release the next trading day" — fetched (raw, whole page).
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md — "Only `limit` orders are
  supported during the overnight session" — fetched (raw).
* `alpaca-py@c42add7c0a70` `alpaca/trading/requests.py:624-670` — `TrailingStopOrderRequest`, one of
  `trail_price` and `trail_percent`; `:388`, `position_intent` on every order request; `:307-335`,
  `ReplaceOrderRequest.trail` and its `client_order_id`; `alpaca/trading/client.py:422-436`, `get_clock`;
  `alpaca/trading/models.py:410-424`, `Clock` — resolved.
* `nautilus-trader@d53849b216e2` `crates/trading/src/strategy/mod.rs:359-404` — `modify_order` fails
  when "quantity, price, and trigger were either None" or unchanged — resolved.
* The same pin, `crates/model/src/orders/mod.rs:116-124` and `:874-878` — a trailing-stop-market has
  "no intermediate TRIGGERED state"; `crates/model/src/orders/trailing_stop_market.rs:532-539`, an
  update sets the trigger price and quantity only; `python/nautilus_trader/model/__init__.pyi:4862-4898`,
  `OrderStatusReport` with no high-water mark; `crates/risk/src/engine/mod.rs:2367-2397`, the cached
  trade — resolved.
* https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md — the replace's body has
  `client_order_id`, "A unique identifier for the new order. Automatically generated if not sent. (<= 128
  characters)", `maxLength` 128 — fetched (raw). ADR 0042, paper, 2026-11-10 — a replace that keeps the
  order's own ID is refused, 422 "client_order_id must be unique" — read.
* The same pin, `docs/integrations/interactive_brokers.md:446-447` — "A modify that sets a trailing
  stop's trigger must pass `params={"trailing_offset": <offset>}` in the order's offset units" —
  resolved. Our adapter takes the parameter with no trigger price; the convention is borrowed.
* `src/canon_trading/session_window.py`, `SLEPT_SECS` — `time.monotonic` here "stops while the machine
  sleeps, as the wall clock does not" — read.
* ADR 0111 on `main` — a node-sent sell beyond the holding, "403 40310000 insufficient qty available" —
  read. ADR 0119 — read on its own branch; it lands apart from this record, as pull request 322, and
  was not on `main` when this was written. ADR 0105 — not on `main`.
* evidence-verifier, 2026-11-12: a kept clock answer could outlive its close across a sleep (fixed, a
  failing test first); five corrections to the text and three to its sources. Applied above.
* That Alpaca enforces `sell_to_close` on the market order a trigger creates, or that a long-only
  account refuses that order beyond the holding — UNVERIFIED.
* A `GTC` trailing stop electing at the next session's open — UNVERIFIED.
* That Alpaca takes a new client order ID named on a replace — read on paper, 2026-11-13, for a limit
  order (`packages/canon-alpaca/tests/test_paper_replace.py`) and for a trailing stop (the proof).
* The venue ID and type of the order an election fills, and its fill on paper — read on paper,
  2026-11-13, one election of a stop never replaced: the stop's own order. After a replace — UNVERIFIED.
* Alpaca's smallest `trail_price`, "0.1% of the stock price" — read on paper, 2026-11-13, in its refusal
  (422, code 42210000). https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Trailing Stop
  Orders", states no smallest trail; its only stated minimums are the price increments of limit and
  stop prices and a stop-loss leg's one cent — fetched (raw, whole page), 2026-11-13.
* A second sell for shares a resting trailing stop holds, on an account that allows shorting — read
  on paper, 2026-11-13: refused, 403, code 40310000, marked and plain. A trailing stop for more shares
  than are held on such an account, and a marked one with no share held — read on paper, 2026-11-13, by
  a one-off probe over REST: refused, 403 40310000 and 422 42210000, for the `DAY` form; the `GTC` form
  — UNVERIFIED.
* That Alpaca sends a `replaced` trade update for a replaced trailing stop — UNVERIFIED.
* A submit that crosses the close in flight — UNVERIFIED.
