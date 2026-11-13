---
status: proposed
date: 2026-11-13
decision-makers: repo owner (requirement, 2026-11-13, as the lead relays it); lead orchestrator (Claude) for its seven clauses; master orchestrator (Claude) for the record, under the owner's delegation (ADR 0074)
review-by: 2027-02-11
---

# No position without its exit, in any session, on any account: seven clauses, each held by a record and marked built, in review or not built; open: the clauses not built

## Context and Problem Statement

On 2026-11-13 a long went over the close with no working exit, as the lead relayed it (`UNVERIFIED` here; the day's reflection records it). The owner's
requirement the same day is that no position exists without its exit, in any session and on any
account. The parts that make an exit already sit in several records (ADR 0083, 0088, 0091, 0119, 0142,
0146, 0148), each with its own scope. This record states the requirement once, as seven clauses, cites
what holds each, and marks each built, in review or not built as of 2026-11-13, so every unit names the
clause it makes true.

## Decision Drivers

* The requirement is the owner's; this record restates it and invents no mechanism.
* Each clause cites the record that decides its means; where none does, the clause is not built.
* Paper only (rule 8): every clause is built and proven on paper first.

## Considered Options

1. A record of its own that states the requirement and maps each clause to its records and units.
2. An amendment to ADR 0146, the 24/5 protection record.

## Decision Outcome

Chosen: **option 1**. ADR 0146 decides how a long is protected around the clock in one node per feed;
clauses 2, 4 and 5 reach past it (the owner's holdings, the gain lock, the re-entry), so the
requirement needs a home that cites 0146 rather than one that stretches it. The rule is the first
practice of the Protect move in the handbook.

| # | Clause | Held by | As of 2026-11-13 |
|---|---|---|---|
| 1 | Every order of ours that opens or adds to a long carries its trailing exit: armed at the first fill, held by our engine, only rising, around the clock on weekdays. An entry whose exit cannot be armed is not sent. | philosophy.md move 3; ADR 0083; ADR 0088; ADR 0146 | Built for the Mover inside a window, held by the engine and only rising, but armed at the next one-second step after the fill, not at the fill; around the clock not built (the lead's plan, units 5, 7 to 9, on branches); for other templates not built (units 11, 12); the refusal partly built: an entry whose trail cannot be planned is refused, but not one whose arming fails |
| 2 | A holding the owner opens gets the same exit within seconds, by itself. One that cannot be planned is named unprotected at once and every minute until it is, never silently. | philosophy.md, Custody; ADR 0142; ADR 0146 | By hand only (ADR 0142's protect task, a broker stop); by itself not built (the plan's unit 10, in work); the minute-by-minute naming not built |
| 3 | The exit is a fast sell: a managed limit that chases within its cap on a confirmed breach; after hours, a limit marked for extended hours. | philosophy.md move 3; ADR 0088; ADR 0119 | Partly built for the Mover: the chase and its floor (ADR 0088) and every protective sell marked for extended hours (the plan's unit 6) are in effect; the confirmed breach (ADR 0119) is in the class behind arms that are off and refused under every paper window's sizing, so a window's sell elects on one quote: not in effect |
| 4 | Once the gain covers its costs, it is locked. | philosophy.md move 3; ADR 0083 | Not built on `main` (ADR 0088 lists it as a departure); a unit in review, as relayed (`UNVERIFIED`) |
| 5 | Every protective sale arms a re-entry on a reclaim while the name still ranks. | philosophy.md move 4; ADR 0088; ADR 0091 | Built for the Mover's own sales inside a window, by the clock and state arms, unranked names refused (ADR 0088, 0091); after the protect lineage's sale on account C, ADR 0148 proposes re-entry triggered by the sale, not by a ranking, so "while the name still ranks" is not met there |
| 6 | The exit survives window ends, restarts and feed changes. Only a market break ends a holding. | ADR 0146 ("only a market break ends a holding" is the owner's, in no record yet) | Not built: the carried level's rebuild is on `main` with no caller yet (the plan's unit 4); the carry, the claim at start and the hand-over between feeds are on branches (units 7 to 9) |
| 7 | The broker's own stop is only the backstop. | philosophy.md move 3; ADR 0129; ADR 0146 | Not met on account C: there the protect task's broker stop is the only cover ADR 0142 provides, and it does not trigger outside the regular session, so after the close C has no working exit (whether a stop rests there now is `UNVERIFIED`); the engine's stop replaces it with the plan's unit 10 |

### Consequences

* Good: one sentence, with its clauses, that every unit of the 24/5 plan and every protection unit
  names; a reader sees at once which clauses are still open.
* Good: a gap like the day's is named against a clause, not rediscovered.
* Bad: a status column goes stale; it is restated in this record whenever a unit lands that changes it.
* Open: clauses 1 (at the fill, outside a window, beyond the Mover, and the arming refusal), 2 (by
  itself, and the naming), 3 (the confirmed breach in effect), 4, 5 on account C, 6 and 7 on account C
  are not built.

### Confirmation

* The Protect move's first practice names this record (`tests/test_handbook.py` checks that it names a
  record that exists).
* Each unit's report names the clause it makes true, and the row in the table above moves with it.
* Paper evidence per clause, as ADR 0146's Confirmation lists for clauses 1, 3 and 6.

## Evidence

Read 2026-11-13.

* `docs/handbook/philosophy.md` on `main`, "The four moves" and "What never changes" — moves 3 and 4, the
  owner's fill recognised at once and protected — read.
* ADR 0083 (accepted) item 3, the gain lock and the fast sell; ADR 0088 (proposed), the chase and its
  floor, and the gain lock listed among its departures not built; ADR 0091 (proposed), re-entry until
  the wind-down; ADR 0119 (proposed), the confirmed breach; ADR 0129 (proposed), Alpaca's trailing stop
  behind a setting that is off; ADR 0142 (accepted), the protect task's broker stop on account C;
  ADR 0146 (proposed), the 24/5 protection and its units; ADR 0148 (proposed, not yet on `main`),
  re-entry on C — read on `main` or on its branch.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, Trailing Stop Orders — "Trailing stop will
  not trigger outside of the regular market hours", why a broker stop cannot be the exit after the
  close (clause 7) — fetched.
* `git log` on `main`, 2026-11-12 to 2026-11-13 — the protective sell marked for extended hours
  (`4777de7b`) and the carried long's level rebuilt from minute closes (`c73f16f1`) are on `main`; units
  5 and 7 to 12 are not — read.
* Second reader (`evidence-verifier`), 2026-11-13: 13 of 23 claims held; corrected clause 3 (the
  confirmed breach is not in effect: `mover.py:1013-1014,1148-1152`, `session_window.py:761,1742`),
  clause 1's arming time and refusal (`mover.py:243,5029-5116`), the unit numbers (the lead's plan, not
  ADR 0146), clause 5's citation of ADR 0148, and clause 7 on C; marked the incident and C's state
  `UNVERIFIED`.
* The lead's 24/5 plan of 2026-11-13 (its unit table), in the lead's archive — read; the statuses of
  units in work are the lanes' reports as relayed, `UNVERIFIED` until each lands.
