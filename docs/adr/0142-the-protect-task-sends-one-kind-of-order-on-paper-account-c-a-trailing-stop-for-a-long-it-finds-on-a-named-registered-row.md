---
status: accepted
date: 2026-11-13
decision-makers: repo owner (request, 2026-11-13); lead orchestrator (Claude; the terms, 2026-11-13)
review-by: 2027-02-11
---

# The protect task sends one kind of order on paper account C, a trailing stop for a long it finds, on a named registered row; open: no row is calibrated for a standing holding

## Context and Problem Statement

ADR 0114 stored account C's pair and decided that nothing trades on C yet: the tasks that trade there were
to come with the standing protector and its account record, and neither is on `main`. The owner asked on
2026-11-13 for a trailing stop to protect whatever longs C holds, in the regular session, today. C is the
owner's to trade by hand and its settings are the owner's, so a task there must not be able to open or
add to a short, whatever the account allows, sell anything by itself, or touch an order it did not send.

## Decision Drivers

* A long the owner holds on C is not ours to sell: a sale there is not undone.
* Nothing may rest on C's settings; they are the owner's and stay as found.
* The distance is a registered row, never a figure of this task's own (AGENTS.md rule 3).
* What Alpaca was not seen to do is named, not assumed.

## Considered Options

1. One run-and-exit task, `paper:protect`, admitted on C: one trailing stop Alpaca holds per uncovered long.
2. Wait for the standing protector and its account record, neither on `main`.
3. Keep the task's refusal of an account that allows shorting, and run it only on a long-only account.

## Decision Outcome

Chosen: **option 1**. Option 2 leaves a holding unprotected behind a build still in review. Option 3
makes the task depend on a setting that is the owner's to choose. This record adds to ADR 0114 and replaces none of it: the
pair's storage, checks and GET-only read stand as that record has them.

| Part | What it is |
|---|---|
| The task | `mise run paper:protect protect` reads C's pair under C's own lock (`canon-paper-protect.lock`); `tests` is account A; `windows` is refused by name. |
| What it sends | For each long no working sell covers, one trailing stop Alpaca holds: `GTC`, whole shares, marked `sell_to_close`, under a client order ID of the protect lineage. Nothing else: no other sell, no buy, no cancel, no replace. |
| The distance | One of the Mover's registered trail rows, by name (`--row`; `session_window.TRAIL_ARMS`: F2, OWN1, OWN3), never under the spread floor or Alpaca's smallest trail. A send that names no row is refused: there is no default. |
| The cancel | `--cancel` cancels each open order of the protect lineage, in one symbol with `--symbol`, once, and reads it by its own ID until it has ended, twenty reads a second apart at most, whatever the cancel's answer: a refusal or a lost answer is decided by those reads, and only a connect timeout, which proves nothing was sent, is a failure with no read. It asks for no session. It sends no order, takes no row, and leaves every other order named and untouched. Only a stop that ended canceled with nothing filled counts as canceled: one that elected first, one canceled after part of it filled, and one still working after the reads are each a failure by name. A run that ends inside one stop's cancel, Ctrl-C included, names that stop and each it did not reach. The stops it cancels are those Alpaca's open-orders listing shows; when a listing shows none it is read once more a second later, and the line then says what the listing showed, not that none is open: a stop the listing shows on neither read is not canceled. A symbol named with no protect stop in the listing exits 2. A foreign trailing-stop sell is left alone, and its line says it may be a protect stop replaced outside the task. |
| Plan-only | `--plan-only` makes every read of a sending run and prints, for each long, what the stop would be under every row: the distance as a share of the price, about where it would open, and what bound it. It sends nothing. |
| What is left alone | A short; a long any working sell covers in full; a long covered in part by an order that is not ours; a sell whose cover cannot be summed. Each is named. |
| Procedure, not code | An account's first run is plan-only and its lines are read before the run that sends. A run on C is from `main` only and needs the lead's go, read in the step that starts it: naming the account and the row for a run that sends, and the account for one that cancels, which takes no row. |

The task does not rest on the account's long-only setting; it reads it and says it. That a stop never
opens or adds to a short rests on the holding and Alpaca's count of the shares no order holds
(`qty_available`), read in the send step, with the milliseconds between that read and the send as its
window; and on Alpaca holding the shares for a stop that rests. The mark tells Alpaca what the sell is
for and is not what this rests on.

### Consequences

* Good: the owner sees every row's distance on the holding before anything rests on it.
* Good: nothing of C's configuration is read as a guarantee or changed.
* Bad: a stop that rests holds its shares, so Alpaca refuses the holder's own sell of that long until the
  stop is canceled: `paper:protect <account> --cancel` cancels the open stops of the protect lineage,
  every one or one symbol's, and nothing else, and the owner can cancel one in Alpaca's own screen. The
  stop is `GTC`, so it stays after the day and can elect in a later regular session.
* Bad: a trailing stop elects on a trade print at or under its level and then sells at the market.
* Open: the ATR rows (OWN1, OWN3) were built for a fresh momentum entry and are one and three ATRs of
  one-minute bars; F2 is a fixed 2%. No registered row is calibrated for a standing holding; every one is
  UNCALIBRATED. A row for a holding of days closes this, with its evidence.
* Shown on paper since, for the `DAY` form (Evidence, 2026-11-13 19:29 and 19:33 UTC): where shorting is
  allowed, Alpaca refused a trailing stop for more shares than were free, marked or not, and refused a
  marked one sent with no share held. The first is Alpaca's free-quantity check, the second the marking.
* Not shown, each UNVERIFIED: those two cases for a `GTC` stop, the form this task sends; the intent on
  the market order a triggered stop becomes; how a `GTC` trailing stop elects at a later open, and that
  it still holds its shares then (ADR 0129's gap); which price Alpaca measures its smallest trail
  against; that Alpaca takes a trailing stop's cancel outside the regular session, and what it answers
  to a second cancel of a stop pending its first.
* The ATR rows carry the cap arm `own`, which the Mover lets stand only for an entry it sizes. This task
  sizes nothing, so a distance over the 2% cap under those rows is refused by name and the long gets no
  stop under that row.
* The stop steps down onto the price step, so the dollar trail can land over the 2% a row names, by a
  hair on most stocks and by more on a low-priced one. The stop still goes out and its line says so: a
  long with a slightly wider stop is better than a long with none (the lead, 2026-11-13).

### Confirmation

`tests/test_protect.py`, against stand-ins for alpaca-py's clients: every stop is `GTC`, a sell, marked
`sell_to_close`; no case sells by market or limit, opens, cancels or replaces; an account that allows
shorting gets the same stop and is told what it rests on; a short is named and gets no order and no
market data read; a holding that changed, or turned short, since the read gets no stop; a send without a
named row, or with one nobody registered, reads nothing; each row gives its own distance; a distance
under Alpaca's smallest trail is raised to it and the line says so; plan-only prints every row and makes
no submit; the task's flags send only on an exact `false` and a registered row; the task takes `protect`
with C's service and lock; a quote that aged past its bound by the send, a submit answered with an ended
order, and a stop signal during the send are each named and leave no stop counted; a trail the price step
carries over the cap says so. `tests/test_custody.py`: a protect stop is named in the account read and
never canceled by the close-only form.

Paper, account A, 2026-11-13 17:06 UTC, `tests/test_paper_protect_task.py`, one run of eight seconds with
the long-only setting off for the run and restored in it, one share held: plan-only printed all three
rows and left no order; a send that named no row was refused and left none; under F2 one order rested,
read back from Alpaca as a sell of one share, `trailing_stop`, `gtc`, `sell_to_close`, of the protect
lineage, its trail 2.0083% of Alpaca's own mark; while it rested Alpaca showed none of the share free; a
second run sent nothing and named the long as covered by that stop; the stop was then canceled unfilled,
the setting read back on, the share sold and the account read flat.

**Amendment, 2026-11-13 — the own cap for a found holding (the lead's ruling).** This replaces the
own-cap refusal above: a holding found in the account has no entry size left to choose, so under an
`own` cap arm its per-symbol distance stands, whether selected from ATR or raised to the spread floor,
up to the inclusive `sizing.DISTANCE_CEILING` the handbook names (`src/canon_trading/sizing.py:45`;
`docs/handbook/practices.md:38`). A distance over that ceiling, before or after the price step and
Alpaca's smallest trail, is refused by name and gets no stop; no fixed fraction is substituted.
The spread floor and Alpaca's smallest trail still hold (`src/canon_trading/protect.py:387`, `:424-425`).
`Mover._entry_trail` takes a default-off `found_holding` parameter for this task alone, covering both
own-cap branches (`src/canon_trading/mover.py:5506`, `:5541`, `:5610`); the Mover's entry still sizes
through `size_from_budget`, with its callers unchanged. F2 keeps its existing planning; OWN3 has the
same own-cap arm as OWN1 and retains its registered ATR multiple. Offline tests reproduce both former
refusals, the ceiling and price-step boundaries, the plan-only source line, and the entry guards
(`tests/test_protect.py:574`, `:592`, `:623`, `:654`, `:662`, `:681`); no paper acceptance of this amendment is claimed.

## Evidence

Read 2026-11-13.

* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Trailing Stop Orders" — "Valid time-in-force
  values for trailing stop are “day” and “gtc”"; "Trailing stop will not trigger outside of the regular
  market hours"; `trail_price` is "a dollar value away from the highest water mark"; the page names no
  smallest trail — fetched (a saved copy of the page; a second fetch on the same day came back summarised).
* https://docs.alpaca.markets/us/reference/getallopenpositions.md — `qty_available`: "Total number of
  shares available minus open orders / locked for options covered call" — fetched (a copy saved this
  day, and the page again by this record's second reader, with the same words).
* https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1.md — "Attempts to cancel an Open Order.
  If the order is no longer cancelable, the request will be rejected with status 422; otherwise accepted
  with return status 204"; the page lists those two answers and no other — fetched.
* alpaca-py at `c42add7c0a7026e2ead3ed64e76f4def2b99fda4` — `OrderRequest.position_intent`
  (`alpaca/trading/requests.py:388`), inherited by `TrailingStopOrderRequest` (`:624`);
  `PositionIntent.SELL_TO_CLOSE` (`alpaca/trading/enums.py:590-598`) — resolved.
* Paper, account A, 2026-11-13, a run with the long-only setting on: a `trail_price` of one cent was
  refused with 422, code 42210000, "trail_price must be >= 0.1% of the stock price" — observed by the
  owning lane; on no page of Alpaca's.
* Paper, account A, 2026-11-13, a second run, with the long-only setting off for the run and restored in
  it (ADR 0111's form): with a marked `DAY` trailing stop resting for the one share held, two limit sells
  of that share were refused with 403, code 40310000, one marked and one not. That run stopped part way
  and left its one share, which was sold afterwards under a separate guard; the stop was a `DAY` stop,
  so a `GTC` stop holding its shares is shown only by this record's own paper run, and there only
  within one session — observed by the owning lane.
* Paper, account A, 2026-11-13 19:29 UTC and 19:33 UTC, two runs of a one-off probe that is not kept in
  the repository, the long-only setting off for each run and restored in it (ADR 0111's form), `DAY`
  trailing stops with a 20% trail. With one share held, a stop for two shares was refused with 403, code
  40310000, "insufficient qty available for order (requested: 2, available: 1)", marked `sell_to_close`
  and unmarked alike. With no share held, a stop for one share marked `sell_to_close` was refused with
  422, code 42210000, "position intent mismatch, inferred: sell_to_open, specified: sell_to_close". The
  first run also repeated the two refused limit sells above. The account read flat and long-only by hand
  after each run. The probe's known limits, found by an outside reader and not fixed: on a lagging or
  incomplete read of Alpaca's it can call the setting restored when it is not, or leave a part-filled
  position or an order of an earlier run unnamed, and it assumes no short in another symbol at its
  start; the two reads by hand after each run cover them — observed by the owning lane.
* ADR 0114 — account C's pair, its service, and "nothing trades on it yet" — read. ADR 0111 — a
  reduce-only sell goes to Alpaca as `sell_to_close` — read. ADR 0129 — the adapter's own venue stop,
  which keeps its long-only bound — read.
* https://mise.jdx.dev/tasks/task-arguments.html — "A boolean flag is `true` when passed"; "Without a
  `default`, it is unset when not passed" — fetched.
* The `evidence-verifier`, 2026-11-13, on this decision as first written into ADR 0114 and then on this
  record: the code, the tests, the pages and the alpaca-py claims hold. Its corrections are taken here:
  the decision is a new record and not a rewrite of an accepted one; the two paper runs are two lines; the
  second run is said to have stopped part way; a `GTC` stop holding its shares is named as not shown
  before; the first run being plan-only is said to be procedure; `qty_available` has its source; and no
  setting or holding of the account is named.
