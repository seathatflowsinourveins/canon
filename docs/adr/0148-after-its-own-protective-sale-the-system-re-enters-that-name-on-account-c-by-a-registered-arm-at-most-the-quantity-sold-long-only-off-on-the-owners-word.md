---
status: proposed
date: 2026-11-13
decision-makers: repo owner (requirement, 2026-11-13, as the lead reads it: after the system's own protective sale, it re-enters that name); lead orchestrator (Claude) for the bounds (2026-11-13); master orchestrator (Claude) for the composition, under the owner's delegation (ADR 0074)
review-by: 2026-12-13
---

# After its own protective sale, the system re-enters that name on account C by a registered re-entry arm, at most the quantity it sold, long only, never after the owner's own sale and off on the owner's word; open: the sale row, the protective sale's lineage and a live feed for two arms

## Context and Problem Statement

Account C holds the owner's longs, and the system's only order there is a protective trailing stop for a
long it finds (ADR 0142). When that stop sells, the name is gone until the owner buys it back by hand, even
when the move that elected the stop reverses in the next minutes. The owner's requirement of 2026-11-13, as
the lead reads it, is that the system re-enters a name after its own protective sale by a registered
re-entry rule. That makes the system a buyer on C, a new kind of order there, so it starts as a record.

## Decision Drivers

* The system undoes only what it did: it buys back a name it sold itself, never more than it sold.
* Long only, held by the code: C allows shorting, and nothing here rests on the account's setting (ADR 0142).
* The owner's own trades and word come first: a sale of the owner's stops it, and the owner can turn it off.
* Reuse what the strategy already has: the Mover's re-entry state and its registered arms (ADR 0088, ADR
  0091, ADR 0096's amendment 7), with any departure named.
* Nothing is built before this record is on `main`.

## Considered Options

1. No re-entry: the owner re-enters by hand, as today.
2. Re-enter through a Mover window, which re-enters only a name its fresh ranking holds.
3. Re-enter one name from a recorded protective sale, by the Mover's re-entry state and arms, triggered by
   the sale rather than by a ranking.

## Decision Outcome

Chosen: **option 3**. Option 1 leaves the requirement unmet. Option 2 cannot serve it: the Mover's live
re-entry answers "the ranking is stale" or "no ranking yet" before it reads a sale, and compares the run its
entry arm qualified, so a name sold outside a ranked window never re-enters (`src/canon_trading/mover.py`,
`Mover._refusal`).

**The bounds.** Each is a refusal in code with a failing test first, and each is named when it refuses.

* **The sale row.** A re-entry needs a row recorded from the protective sale's own fill: the exact sell
  order's client order ID, the time of its last fill, its average fill price and the quantity filled. No
  protector keeps these today: the protect task sends a stop and exits (ADR 0142), and the standing
  protector, as built (not yet on `main`, `UNVERIFIED`), drops the holding on its fill. With no row there
  is no re-entry. The Mover's exit record and
  its re-entry report are the shape.
* **Only a name it sold itself.** The row is written only for a sell of the protective lineage, and the
  re-entry is keyed on the exact sell order recorded, not on a lineage alone. A protective sale is named
  in the protect lineage (`canon-protect-`, `src/canon_trading/custody.py`), the standing protector's
  included (this record's choice): an order the engine names in the node's form reads as any node trade,
  so it cannot tell a protective sale from another. The engine's trailing stop takes a client order ID at
  the pin; whether the emulator keeps it on the order it releases is `UNVERIFIED`. A stop so named also
  counts as cover for the protect task, as its own stops do.
* **At most the quantity it sold, once.** A row is re-entered at most once, by one buy of its filled
  quantity in whole shares, less any of it already held again; the row is then spent. A name is re-entered
  at most once per trade date (`reentry_per_day`, 1, a new row, UNCALIBRATED, this record's), so a long
  sold, re-entered and sold again waits for the next trade date.
* **The buy's own lineage.** The re-entry buy is sent in the protect lineage with a re-entry mark in its
  client order ID, so custody reads it as ours and the bound below can explain the position by it.
* **Long only.** The re-entry sends a buy, sized from the row and from what is held, and never a sell; a
  buy that would leave any short in the name is refused. C's `no_shorting` is read and reported, never
  relied on.
* **Never after the owner's own sale.** A sell of the name by any order not of our lineages, after the
  protective sale or before it on the same trade date, closes the row with no re-entry; so does any change
  in the position that our orders do not explain.
* **The sale's trade date.** A row is open only on the New York trade date of its sale and closes at its
  end, 20:00 (the clock arm's trade-date rollover; the expiry is this record's choice). So the `clock` arm,
  whose cooldown is 300 seconds, cannot re-enter a sale in the last five minutes before 20:00.
* **Off on the owner's word.** The re-entry is a setting, off by default; only the owner turns it on, and
  the owner's word turns it off at once (a procedure, not a test). A run on C that may send a buy needs
  the lead's go naming the account and the row, as ADR 0142's sends do.
* **The arms and their rows.** The registered re-entry arms, with their rows UNCALIBRATED as registered:
  `reentry_arm` (default `clock`) and `cooldown_secs` 300, the clock arm of ADR 0088; `reclaim` and `turn`
  with `turn_k` 2 and `turn_d` 1, ADR 0096's amendment 7, which gives them no time cooldown. `clock`,
  `reclaim` and `turn` need only the row's price and time, the trail distance the protective stop was sold
  at, and the name's complete one-minute closes, so they can run for one name. One departure: the
  registered `turn` asks that the name is still ranked and eligible, and a name the owner holds has no
  ranking, so here it asks only that the name is still tradable; the measure judges the arm so changed. `vwap` and `opening` are replay-only
  here: no live writer supplies the session VWAP on `main`, and every paper window refuses the opening arm
  until the 09:30 bar request lands.
* **The trigger and its grain.** The protective sale starts the re-entry state, not a ranking or a Mover
  slot. The first close that counts is the first complete one-minute close whose interval ends after the
  sale's last fill; the replay is held to the same grain, since today it starts one bar later for
  `reclaim`, `vwap` and `clock` (for `turn`, live and replay already seed the same close).
* **Protected again at once.** A re-entered long is a long like any other, protected by the protector
  that sold it (ADR 0146; the standing protector is not yet on `main`, `UNVERIFIED`).

**The measure** is ADR 0147's (proposed, landing beside this record): re-entries after a stop, against holding flat, with their counts.

**What no upstream gives.** Alpaca holds a trailing stop but has no order that buys back after it fills,
and NautilusTrader's emulator and strategies carry no re-entry after a stop; the re-entry state is this
repository's own, already registered for the Mover. This record reuses it and adds no upstream.

### Consequences

* Good: a protective sale on a move that reverses can be undone, by the rule the Mover already runs, within
  what the system itself sold.
* Bad: the system becomes a buyer on C, so a defect in a bound buys the owner's account a long; each bound
  is a refusal with its own test, and the setting is off until the owner turns it on.
* Bad: `vwap` and `opening` are not available for one name until their live feeds exist.
* Bad: a re-entry can be sold again by the protector; the once-a-day row bounds the churn, and the measure
  counts it.
* Bad: on C custody freezes every held position, so `paper:close` cannot undo a re-entry made in error; the
  owner, or the protector's stop, does.

### Confirmation

* A failing test first for each bound: no row, a row of another lineage, more than the quantity sold, a
  second re-entry of a row or of a name on one trade date, the buy's client order ID read as ours, a buy that
  would leave a short, the owner's sale before and after, a changed position, the trade date's end,
  the setting off, `vwap` and `opening` refused, and the first counted close.
* A paper run on C, with the lead's go, in which one protective sale is followed by at most one re-entry of
  at most its quantity, and the owner's sale stops it.
* The record stays `proposed` until those hold and the owner has turned the setting on once.

## Evidence

Read 2026-11-13, in the tree at `main`, unless a URL is given.

* https://docs.alpaca.markets/us/docs/orders-at-alpaca — a trailing stop that Alpaca holds; bracket and OTO
  orders, whose legs are exits of an entry; OCO, "only exit order is supported", added after the position is
  open; no order type buys after an exit fills — `fetched`, checked by the evidence-verifier.
* https://nautilustrader.io/docs/llms.txt — the advanced orders, emulated orders and strategies pages (the
  `latest` docs, not the pin): no built-in re-entry after a stop; contingent orders release children on a
  fill, a building block, not a re-entry rule — `fetched` by the evidence-verifier.
* ADR 0088 — the clock arm and its cooldown; ADR 0096's amendment 7 — `reclaim`, `turn` and `vwap` with no
  time cooldown, and `turn`'s ranked-and-eligible condition.

* `src/canon_trading/mover.py` — `TurnState` and `ReentryState` (the sale's price and time, the trail
  distance in force, complete closes after the sale); `Mover._refusal`'s sale branch (the stale-ranking
  refusal before the sale, the arms' predicates); the exit record and the re-entry report; the re-entry
  rows (`reentry_arm`, `cooldown_secs`, `turn_k`, `turn_d`, the opening rows), UNCALIBRATED; the long-only
  construction; `session_vwaps`, read for each close and written by no module under `src/`.
* `src/canon_trading/reentry_replay.py` — `reentry_signal`, seeding the turn's low with the sale bar's close
  and starting on the closes after it.
* `src/canon_trading/session_window.py` — the window refuses `vwap` re-entry and any opening arm.
* `src/canon_trading/custody.py` — `lineage`: the node form and `canon-close-` are ours, `canon-protect-`
  is the protect lineage, anything else foreign; an account whose `no_shorting` is false freezes a position.
* ADR 0142 — the protect task's one kind of order on C, the lead's go, and that the task does not rest on
  the account's long-only setting; ADR 0114 — account C's pair; ADR 0129 — the identity of a held stop's
  fill is open; ADR 0091 — re-entry until the wind-down; ADR 0146 — the protector; ADR 0147 — the measure,
  proposed and not yet on `main`.
* The standing protector, not yet on `main`, read as its build: it submits its stop with no client order ID
  of its own and keeps no sale on its fill — `UNVERIFIED` until it lands.
* A code reading of 2026-11-13 found the five gaps this record closes: the missing sale row, the ranking
  before the sale, `vwap` and `opening` with no live feed, the protective sale's lineage, and the one-bar
  grain between live and replay.
