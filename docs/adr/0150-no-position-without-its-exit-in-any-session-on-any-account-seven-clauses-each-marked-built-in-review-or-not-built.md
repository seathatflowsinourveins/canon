---
status: proposed
date: 2026-11-13
decision-makers: repo owner (requirement, 2026-11-13, as the lead relays it); lead orchestrator (Claude) for its seven clauses; master orchestrator (Claude) for the record, under the owner's delegation (ADR 0074)
review-by: 2027-02-11
---

# No position without its exit, in any session, on any account: seven clauses, each held by a record, with each clause's status printed from the case set's marks; open: the clauses printed not built or with no case

## Context and Problem Statement

On 2026-11-13 a long went over the close with no working exit, as the lead relayed it (`UNVERIFIED` here; the day's reflection records it). The owner's
requirement the same day is that no position exists without its exit, in any session and on any
account. The parts that make an exit already sit in several records (ADR 0083, 0088, 0091, 0119, 0142,
0146, 0148), each with its own scope. This record states the requirement once, as seven clauses, cites
what holds each, and leaves each clause's status to a command that reads the case set, so every unit
names the clause it makes true and no status here goes stale.

## Decision Drivers

* The requirement is the owner's; this record restates it and invents no mechanism.
* Each clause cites the record that decides its means; its status is what its cases show, not a word kept here.
* Paper only (rule 8): every clause is built and proven on paper first.

## Considered Options

1. A record of its own that states the requirement and maps each clause to its records and units.
2. An amendment to ADR 0146, the 24/5 protection record.

## Decision Outcome

Chosen: **option 1**. ADR 0146 decides how a long is protected around the clock in one node per feed;
clauses 2, 4 and 5 reach past it (the owner's holdings, the gain lock, the re-entry), so the
requirement needs a home that cites 0146 rather than one that stretches it. The rule is the first
practice of the Protect move in the handbook.

Each clause's status is printed, not kept here: `uv run --locked python -m canon_trading.exit_rule` runs
the case set (every test marked `no_position_without_its_exit(clause=N)`) and prints each clause as
`built` (it has cases and every one passes), `not built` (one of its cases is a strict expected failure,
whose reason is the open gap) or `no case`, and exits 1 when a case is broken: one that fails or is skipped, one
that passes while still marked an expected failure, an expected failure with no strict mark (an imperative
`pytest.xfail` among them), a case naming no clause, or a set with no case at all.
"In review" is not a state the marks show: run on a unit's branch, the command prints what that unit
would make built. A clause is built only as far as its cases reach; a gap this record's first version
named without a case (the arming at the fill, around the clock and outside a window; the confirmed
breach in effect; clauses 4 to 7) is seen once its case is written.

| # | Clause | Held by |
|---|---|---|
| 1 | Every order of ours that opens or adds to a long carries its trailing exit: armed at the first fill, held by our engine, only rising, around the clock on weekdays. An entry whose exit cannot be armed is not sent. | philosophy.md move 3; ADR 0083; ADR 0088; ADR 0146 |
| 2 | A holding the owner opens gets the same exit within seconds, by itself. One that cannot be planned is named unprotected at once and every minute until it is, never silently. | philosophy.md, Custody; ADR 0142; ADR 0146 |
| 3 | The exit is a fast sell: a managed limit that chases within its cap on a confirmed breach; after hours, a limit marked for extended hours. | philosophy.md move 3; ADR 0088; ADR 0119 |
| 4 | Once the gain covers its costs, it is locked. | philosophy.md move 3; ADR 0083 |
| 5 | Every protective sale arms a re-entry on a reclaim while the name still ranks. | philosophy.md move 4; ADR 0088; ADR 0091 |
| 6 | The exit survives window ends, restarts and feed changes. Only a market break ends a holding. | ADR 0146 ("only a market break ends a holding" is the owner's, in no record yet) |
| 7 | The broker's own stop is only the backstop. | philosophy.md move 3; ADR 0129; ADR 0146 |

### Consequences

* Good: one sentence, with its clauses, that every unit of the 24/5 plan and every protection unit
  names; one command shows a reader which clauses are still open.
* Good: a gap like the day's is named against a clause, not rediscovered.
* Good: no status is kept by hand, so none goes stale: the command reads the case set as it stands.
* Bad: a clause reads `built` when its cases pass, however few they are; a gap is seen only once its case
  is in the set.
* Open: the clauses the command prints `not built` or `no case`.

### Confirmation

* The Protect move's first practice names this record (`tests/test_handbook.py` checks that it names a
  record that exists).
* Each unit's report names the clause it makes true. A landing that closes a gap removes its case's
  expected-failure mark in the same pull request, and updates only its own practice file
  (`docs/handbook/practices/`, one file each); this record and its table are not edited for a status.
* `tests/test_exit_rule.py` runs the command on the repository's own set and fails if it is broken, and
  checks that this record keeps no status column.
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
