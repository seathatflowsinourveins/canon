---
status: proposed
date: 2026-11-12
decision-makers: repo owner (requirement, 2026-11-12); lead orchestrator (Claude)
review-by: 2026-12-12
---

# The entry switch is the strategy's own arming file, read at each buy decision; open: a strategy that reads it, the tracked live file, and its paper proof

## Context and Problem Statement

ADR 0110's change (e) asks for an arming switch that makes every strategy submit no buy while exits keep
working, and its gap (i) names the strategies' own arming file as that switch, re-entries included, with
no record of the file's reader, format or defaults. Upstream's own means is the `REDUCING` trading state,
which admits only reduce-only orders, and our adapter refuses every reduce-only order unless a node turns
a setting on (ADR 0111). This record is the switch's own.

## Decision Drivers

* A sell must never wait on, or be stopped by, the switch.
* The switch does not depend on the venue or on the adapter's reduce-only support.
* A switch set for live never gates accounts A and B, whose nodes buy by themselves (the owner's
  requirement, 2026-11-12, recorded here; no earlier record states it).
* An automatic re-entry is an automatic buy: on account C and on live it is off until the arming file
  says on (the lead, 2026-11-12).
* One reader, so every strategy reads the file the same way.

## Considered Options

1. Upstream's `REDUCING` trading state.
2. The strategy's own arming file, read immediately before it would submit a buy.
3. Upstream's `HALTED` trading state.

## Decision Outcome

Chosen: **option 2**, `proposed` until the gaps below close. Option 1 admits a sell only when it "sets
`reduce_only=true`", which the adapter refuses by default; it may be added later as a second layer on
top of this switch (ADR 0111). Option 3 "denies released orders, including protective stops".

| Part | What it is |
|---|---|
| The reader | `src/canon_trading/arming.py`: `read_arming_for(account, path)` returns `Arming(armed, read)`; `read` is the text a strategy's record keeps. |
| What it stops | Every buy a strategy would submit by itself, an entry and a re-entry alike. |
| The format | The first line is the value and nothing else, `on` or `off`; the rest is a note, as in `canon/paper-schedule`. |
| When it is read | At each buy decision, immediately before the buy would be submitted. Nothing is kept between decisions, and nothing writes the file. |
| A live node | Reads `canon/live-arming` under the checkout of `main` (`live_arming_path`), as `canon/paper-schedule` is read from `main`, never from a branch's checkout. Only a landing on `main` changes it, so only the owner's word does. An absent file is off. |
| Account C (`protect`) | Off until a file says on, like live: no file, or an absent one, is off. |
| Accounts A and B | Name no file unless a run gives one. No file, or an absent one, is on. |
| A file that is unusable | Unreadable, empty, a first line that is neither value, a path whose folder is missing, or a link to nothing: off, by name, on every account. A mistyped path is therefore off, not absent. |
| Sells | Never read it. |

**Gaps, and what would close each.**

* **No strategy on `main` reads it.** The reader and its tests land here; an arming file that says off
  stops nothing today. Closed by each strategy's landing with a test that a buy is not submitted while
  the file says off, that the stopped buy is recorded, and that a protective sell still goes out.
* **`canon/live-arming` is not tracked.** ADR 0110 makes it a tracked file on `main`, off by default; no
  such file exists, and for a live node its absence is off. Closed by the live lane's landing.
* **The paper proof** ADR 0110's gap (i) names: with the switch off, a qualifying entry is not submitted
  and a due protective sell goes out.

### Consequences

* Good: stopping entries needs nothing from Alpaca or from the adapter, and cannot stop a sell.
* Good: the default is the safe one for each account: on for A and B, off for C and for live.
* Bad: it is each strategy's duty to call the reader at every buy; a strategy that does not is not
  stopped. The tests named in the first gap are what holds that.
* Bad: the file is read at every buy decision, a small read on the entry path.

### Confirmation

`tests/test_arming.py`, 28 cases: the two values; a note after the first line; an absent file and no file
under each default; an empty file, a first line that is neither value and an unreadable file, each off
by name; the read text for each; the live file's absolute path under the checkout it is given; the
default by account (on for `tests` and `windows`, off for `protect`, live and any other name); account
C buying only once its file says on; and a missing folder and a link to nothing, each off by name.
The last eight failed before the reader had them. Run in `scripts/ci`.

## Evidence

Read 2026-11-12.

* `nautilus-trader@d53849b216e2` `docs/concepts/execution/index.md:278-303` — `REDUCING` permits "Eligible
  individual reduce-only submissions, cancels, and queries"; "a protective stop must set
  `reduce_only=true`"; "`HALTED` denies released orders, including protective stops" — resolved.
* https://nautilustrader.io/docs/md/latest/concepts/execution/index.md — the same three sentences on
  the current page — fetched (through a summarising fetch).
* ADR 0110 on `main` — change (e), "An arming switch, a tracked file on `main` in the manner of
  `canon/paper-schedule`, off by default"; gap (i), the strategies read the arming file at each decision
  and submit no buy while it says off, sells untouched — read.
* `canon/paper-schedule` on `main` — "The first line is the value; the rest is this note" — read.
* ADR 0111 — the adapter refuses every reduce-only order unless `reduce_only_sells` is on — read.
* ADR 0050:225 on `main` — `canon/paper-schedule`, "which `paper:session` reads at every scheduled
  firing, from the main that firing has"; ADR 0065:115, "The main checkout moves to origin/main with
  `git merge --ff-only`" — read.
* evidence-verifier, 2026-11-12: account C must default to off, re-entries named; the live file read
  from `main` and who changes it; the reader's text about a setting that does not exist; the unsourced
  "paper is never gated"; a mistyped path read as absent; and the Context's account of ADR 0110's gap
  (i). Applied above, the two code changes with failing tests first.
* That a strategy's buy path honours the switch on paper — UNVERIFIED until the first strategy that
  reads it lands.
