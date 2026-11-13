---
status: proposed
date: 2026-11-13
decision-makers: repo owner (requirement, 2026-11-13); lead orchestrator (Claude) for the working decisions
review-by: 2026-12-13
---

# A long is protected around the clock on weekdays by a level rebuilt at each start, in one node per feed, the engine holding the stop; open: a protection part reused across strategies, which no upstream shows, the restart proof and the measurements

## Context and Problem Statement

The owner's requirement of 2026-11-13: the trailing stop protects a long around the clock on weekdays, with
no seam between session parts, and is the foundation further strategy templates are built on, each with
protection applied automatically; it is not deferred and starts that day.

Alpaca's own trailing stop "will not trigger outside of the regular market hours", and outside it only a
DAY or GTC limit marked `extended_hours` is accepted. So the project holds the stop in NautilusTrader's
order emulator and sends a limit. A node runs for a bounded window, so a holding outlives the node that
armed its stop. Where does the stop's level live across a restart, how many nodes cover a day, and who
holds the stop in the regular session?

## Decision Drivers

* No seam: a long held across a session boundary or a planned restart has a stop again at once.
* One stray observation never raises the level or sells (ADR 0119).
* Use what the pinned upstreams document; adopt nothing to carry the level (rule 3).
* The custody rule: no foreign order is canceled or resized.

## Considered Options

1. The level in NautilusTrader's cache database, the emulated order restored at start.
2. The level in strategy state (`on_save`/`on_load`).
3. Alpaca's own GTC trailing stop as the carrier between nodes.
4. The level rebuilt at every start from the protection's start, its trail row and the minute closes since.

## Decision Outcome

Chosen, as working decisions pending the proofs below: **option 4**, in one node per feed, with the engine
holding the stop in every part (B1).

**The level is a pure function of start, row and tape.** It is computed from the protection's start, the
trail row and the minute closes since the start, fetched with `request_bars` naming the feed, and passed as
the emulated sell's initial `trigger_price`. The engine seeds its trail from that price and only raises a
sell's trigger after it. It reads closes, not highs, to narrow what one stray print can do; a close is
itself a print, so how much that buys is `UNVERIFIED`. The cost: the rebuilt level can be looser than the
bid-high the live engine reached.

Options 1 and 2 are rejected for now. A restored emulated order is rebuilt with no params, so the
extended-hours mark is lost, and option 1 needs a cache database adopted (Redis or Postgres). Option 2
needs a Redis backing, and strategy state is saved at most once per run.
Option 3 holds in the regular session only and reserves the shares (ADR 0142); whether Alpaca's mark moves
outside it is `UNVERIFIED`.

**Per-feed nodes.** The adapter keeps one feed per stream and refuses a subscription naming another;
pre-market, regular and after-hours use `sip` and overnight `boats` (`MOVER_PARTS`). So a day is two feed
spans, 04:00-20:00 on `sip` and 20:00-04:00 on `boats`, and one node runs each span whole: two nodes and
two hand-overs a day, both outside the regular session, each a planned restart. For a node that carries a
long this amends ADR 0044's bound of 60 minutes a window (the lead's ruling of 2026-11-13); every other
window keeps it. ADR 0044's amendment of 2026-11-13 narrows this: until it is relaxed, only a by-hand
paper run under a flag of its own runs past 60 minutes, and no scheduled window does, so the two nodes
a day are by hand for now. A node running sixteen hours has not been run, so its soak (memory, the stream across
hours, reconnects) is `UNVERIFIED` until it is. The adapter resets its feed on a reconnect, so one node
switching feeds without a restart may be possible; that is not tried.

**B1: the engine holds the stop all day.** The protective sell is always marked `extended_hours`, so a sell
armed before 16:00 still works after it. No broker stop rests while a node is alive. That a marked limit is
accepted and fills in the regular session is `UNVERIFIED` on paper. A dead node has no stop (ADR 0110 (d)),
outside this record. A broker backstop in the regular session (B2) waits for the cancel-latency
measurement; a plain stop moved to the engine's level (B3) is rejected, since Alpaca's wash-trade
protection refuses a new order while an opposite-side stop is open (ADR 0110).

**The gap (rule 3).** Upstream does show a strategy arming its own trailing stop when its entry fills:
NautilusTrader v1's example `ema_cross_trailing_stop.py` and an acceptance strategy at the pin. The lead's
plan said no upstream example pairs a strategy with an automatic trailing stop; that is corrected here.
What no upstream shows is a protection part reused across strategies, with its level carried over a
restart. Under `NETTING` a position is kept per instrument and per strategy, and data actors do not manage
orders, so a separate protector strategy for other strategies' longs would book its own short. The
direction is one protection part extracted from the Mover and used inside each strategy, as the upstream
examples arm the stop inside the strategy, after the carry works. Its shape is the project's own until an
upstream shows one.

### Consequences

* Good: no database, no persisted state and no broker order carry the level; a restart rebuilds it.
* Good: one seam rule for every part: a feed hand-over is a planned restart, and the level is rebuilt
  across it; the carry is not built yet.
* Bad: a node now runs up to sixteen hours, far past any window run so far.
* Bad: the rebuilt level can trail the live one, so a long can give back more than the live trail allowed.
* Bad: "since the protection began" needs a durable start time for a long held for days, not yet located.
* Bad: until the template exists, each strategy's protection is the Mover's own.

### Confirmation

This record stays `proposed` until each of these holds, and is then accepted or superseded:

* a rebuilt level never exceeds the highest close less the trail and never falls below its start, on
  shaped bars with a stray high (a test first);
* a slot opened with a given level arms its sell from it, and a level at or above the bid holds and does
  not sell on its first step on one observation;
* a holding survives a window boundary and a planned restart with its sell re-armed from the rebuilt
  level, on paper across one boundary, with the gap from node stop to re-arm and the rebuilt level
  against the live one measured;
* every protective sell is marked `extended_hours`, and a marked limit is accepted and fills in the
  regular session and after-hours, on paper;
* the hand-over at 20:00 and 04:00 between the `sip` and `boats` nodes, on paper from an overnight open;
* a carrying node runs its feed's whole span on paper, and its soak is recorded;
* the Mover delegates to the extracted part with identical orders in a replay, and a second strategy gets
  protection automatically.

The gap closes when an upstream example or documented pattern shows a protection part shared by
strategies with its level carried over a restart, or when the extracted part's record names the upstream
contract it implements.

## Evidence

Read 2026-11-13. NautilusTrader at the pin `d53849b216e2` (`scripts/canon src nautilus-trader`). A
repository path is read in the tree at `05905849`.

* The owner's requirement: the lead's plan of 2026-11-13, kept outside the repository, read.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md: a trailing stop "will not trigger outside of the
  regular market hours" (:337); only DAY or GTC limits are extended-hours eligible (:54); overnight runs
  8:00pm to 4:00am ET (:46); a marked `day` order "can also execute during supported extended hours"
  (:367), which implies the regular session too, not yet shown by a paper fill. `fetched` raw.
* `crates/execution/src/trailing.rs:58-61`: the trail is seeded from the order's current trigger price.
  `:98-101`: a sell's trigger moves only to a higher candidate. Unchanged in v2.0.0rc9. `resolved`.
* `crates/execution/src/order_emulator/emulator.rs:368-376`: a restored emulated order is rebuilt with
  `None` params. `resolved`.
* `docs/how_to/configure_live_trading.md:193-203`: strategy state needs a Redis backing and is saved at most
  once per run; a crash loses every change since. `resolved`.
* `docs/concepts/positions.md:198`, `:237`: under `NETTING`, one position per instrument and strategy.
  `docs/concepts/actors.md:791`: data actors do not manage orders. `docs/concepts/cache.md:139`, `:215`:
  the cache database may be Redis or Postgres. `resolved`.
* The pin's `python/tests/strategies/acceptance.py:514-589` (`_EMACrossTrailingWorkflow`) submits a
  trailing stop on each entry fill (:584). The registered `nautilus-trader-v1` pin's
  `nautilus_trader/examples/strategies/ema_cross_trailing_stop.py:376-385` arms one on the opened position.
  No file under the pin's `examples/` or its Rust example strategies mentions a trailing stop. `resolved`.
* `src/canon_trading/mover.py:3893-3905` at `05905849`: the Mover's emulated trailing sell is sent with an
  initial `trigger_price`, DAY, with an emulation trigger. Read in the tree.
* `src/canon_trading/session_window.py:2354-2359` (`MOVER_PARTS`), `packages/canon-alpaca/README.md:56-61`
  (`request_bars` with a feed in params) and `packages/canon-alpaca/src/canon_alpaca/data.py:222-223`
  (one feed per stream, the first subscription's; another is refused), at `05905849`. Read in the tree.
* ADR 0119:10-16 (one observation elected or would have elected a sale on 2026-11-12, in three ways), ADR 0142:84-94 (Alpaca's stop rested on
  paper with none of the share free), ADR 0110:52 (the dead-node guard, (d)) and :83, :114-115 (Alpaca's
  wash-trade protection). Read in the tree.
* The owner's requirement is worded as the lead's plan of 2026-11-13 words it; the plan is kept outside
  the repository.
* The `evidence-verifier` pass of 2026-11-13 found the decision's sources held and corrected: the gap,
  which the plan overstated (upstream examples do arm a trailing stop inside a strategy); the claim that
  closes stop a stray print; the cache database's backings; a day as two nodes; the restart as already
  exercised; the Alpaca quotes, now fetched; two line numbers and two qualifiers. It added the reconnect
  caveat and the first-step check from the plan. Applied. The lead's ruling of the same day then set one
  node per feed span, amending ADR 0044's 60-minute bound for a carrying node, with the soak `UNVERIFIED`;
  ADR 0044's later amendment of that day limits this to a by-hand paper run under its own flag.
