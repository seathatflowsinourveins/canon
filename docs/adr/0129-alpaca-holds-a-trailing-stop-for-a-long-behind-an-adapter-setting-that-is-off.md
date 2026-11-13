---
status: proposed
date: 2026-11-12
decision-makers: lead orchestrator (Claude) for the unit, its conditions and where it may be used; repo owner ratifies
review-by: 2026-12-12
---

# Alpaca holds a trailing stop for a long, sent by the adapter behind a setting that is off; open: its election on paper, the identity of its fill, and the intent on the order it becomes

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
| The intent | A trailing stop is in `sell_to_close_order_types` only when a node names it there. A reduce-only one is refused otherwise. |
| A start with no cache | An open trailing stop the cache does not hold maps only with `report_external_trailing_stops`; else the start fails by name. Nothing in a client order ID tells one node's order from another's. |

**Where it may be used.** Only on a long-only account, until the paper proof is in this record. There
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

* The intent on the order a trigger creates, and a second sell for shares a resting stop holds: read
  over REST.
* The long-only bound lifts on evidence only (the lead, 2026-11-12): on a paper account that allows
  shorting, a trailing stop marked `sell_to_close` for more shares than are held, and a second sell for
  shares a resting stop holds. If Alpaca refuses both, a venue stop cannot oversell there.
* The venue ID and order type a fill arrives under after the election: a stop with a tight trail left
  to trigger.
* Submit, read back, a change of quantity with the offset, and cancel, through the adapter.
* A `GTC` stop electing at the next open, where Alpaca warns of a worse fill: one held overnight.

### Consequences

* Good: a long has a stop that outlives the node, for the price of one clock request a session.
* Bad: the election is one print inside the NBBO, with none of the counted, spaced prints ADR 0119
  has a strategy wait for. A node uses one or the other for a holding.
* Bad: it becomes a market order, which no test here has seen filled.
* Bad: the stop's level between reports is Alpaca's; the node sees it only in a report.

### Confirmation

`packages/canon-alpaca/tests/test_venue_trailing_stops.py`, 66 cases on a real NautilusTrader node
against the tests' stand-in venue; no request reaches Alpaca. That is a simulation, not a paper run.
They cover: the setting off; each refused form; the two offsets on the wire; the clock open, closed,
unreadable, absent, reused to its boundary and not trusted past it after a sleep; the trail change and
the quantity-only replace; a cancel sent during the clock read; fills under the order's own ID and
under one linked by `replaces`; a fill under an unlinked ID, counted and not adopted; and a start with
and without the cache. Run in `scripts/ci`. The paper proof's reads are added here when they exist.

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
  `ReplaceOrderRequest.trail`; `alpaca/trading/client.py:422-436`, `get_clock`;
  `alpaca/trading/models.py:410-424`, `Clock` — resolved.
* `nautilus-trader@d53849b216e2` `crates/trading/src/strategy/mod.rs:359-404` — `modify_order` fails
  when "quantity, price, and trigger were either None" or unchanged — resolved.
* The same pin, `crates/model/src/orders/mod.rs:116-124` and `:874-878` — a trailing-stop-market has
  "no intermediate TRIGGERED state"; `crates/model/src/orders/trailing_stop_market.rs:532-539`, an
  update sets the trigger price and quantity only; `python/nautilus_trader/model/__init__.pyi:4862-4898`,
  `OrderStatusReport` with no high-water mark; `crates/risk/src/engine/mod.rs:2367-2397`, the cached
  trade — resolved.
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
* The venue ID and type of that order, and its fills on paper — UNVERIFIED.
* A submit that crosses the close in flight — UNVERIFIED.
* Any election of a venue trailing stop on paper — UNVERIFIED; no paper run is part of this record.
