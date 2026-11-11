---
status: proposed
date: 2026-11-11
decision-makers: the owner for the requirement (2026-11-11); master orchestrator (Claude) for the measures, the arms and the method, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# Early-entry study: four measures, fifteen arms and a walk-forward with a holdout opened once, fixed before any result is read; every figure a diagnostic until the ledger takes Mover trials; open until the list of mover days and the gate's cost model arrive

## Context and Problem Statement

The owner decided on 2026-11-11 that the mover strategy must enter at the early stage of a move, not
after the move has largely happened, and that this is shown by backtesting on historical data and tuned
from it. ADR 0083 names the entry (the first rising run of the first leg) and leaves every threshold an
uncalibrated row. What does "early" mean as a number, on which data is it measured, how is a row chosen
without hindsight, and what may a result claim? This record fixes all four before any tape is replayed.

## Decision Drivers

* A measure chosen after a result is seen is tuned to it. The measures, arms and split are written and
  committed first, as ADR 0048 and ADR 0077 did for their studies.
* A row changes only through the gate (ADR 0010, ADR 0080), on trials the ledger records (ADR 0081).
  The ledger records `MaCross` on bars at zero fees (ADR 0067); it takes no Mover trial yet.
* Rule 3: no upstream ships these measures. The code is arithmetic over upstream bars and names what it
  mirrors; nothing here recommends a trade, a position or a strategy.

## Considered Options

1. A pre-registered study on minute bars now, labelled a diagnostic, whose arms become ledger trials
   when the ledger takes Mover trials with a cost model.
2. Wait for that ledger change and measure only by replaying the strategy in a `BacktestEngine`.
3. Tune the rows from the day's tape and the paper windows as they come.

## Decision Outcome

Chosen: **option 1**. Option 3 is tuning by hindsight and is refused. Option 2 measures nothing until
the strategy and the ranking are on `main`, and its protection cannot run on the history there is: the
emulated trailing stop needs a quote or a trade to activate (measured in `mover.py`'s header at
`660b37e`), and historical quotes through the adapter are IEX only (ADR 0066).

**The window.** A name's day is 04:00 to 16:00 New York, on one-minute `sip` bars from the catalog. The
reference close is the latest earlier session's daily bar close, or, where the catalog holds no daily
bar for it yet, the close of that session's last regular minute bar, and each report says which. The
window high is the highest bar high in the window. A minute is the one in which a thing was known: a
bar's close.

**The measures**, each reported with its count:

| # | Measure | Definition |
|---|---|---|
| 0 | Ignition | The first minute the name meets the listing condition under the registered rows: move from the reference close at least 3%, and dollar volume since 04:00 at least 3 times the mean of the same minutes in its 5 earlier sessions. The same clock for every arm. ADR 0083's third starting row, trade acceleration at least 1.5, is left out: there is no trade history to compute it from (Limits). A departure from the registered rows |
| 1 | Entry lead | Minutes from ignition to the entry |
| 2 | Share of the move still ahead at entry | (highest high after the entry's bar − entry price) / (window high − reference close); the share done is (entry price − reference close) over the same |
| 3 | Late | A name never entered, or entered with more than half its move done, as a share of the names replayed; never entered and late are also given apart, and a name with no bars is counted missing, never dropped |
| 4 | Outcome | The entry's return when managed (below) against staying flat, which is zero, and against holding to the window's end, at the half-spread taken from quotes and at twice it |

**The bar model.** The walk is NautilusTrader's own for bars (Evidence): a bar is taken open, high,
low, close, and, without a latency model, an order sent on a bar's close settles against that close. The
entry fills at the signal bar's close plus the half-spread. The managed exit is this study's arithmetic
on that walk, not the engine's: the emulated trailing stop cannot run on bars alone. It mirrors `Mover`'s
protection at its defaults: a trigger 2% of the entry under the highest bid since the entry, a released
limit 0.5% of the entry under the trigger, a chase never below 2% under that limit; a position still
held is sold at the window's last close. An entry whose bid is under the planned stop is refused, as the
paper window's working copy does and `660b37e` does not (UNVERIFIED until that is committed). Only a
name's first entry of the day is replayed.

**The arms**, N = 15: the registered rows, twelve arms that move one row from them, and two that add
the pullback to the 15-minute and the 10% arm (`ARMS` in the module). All values are choices of this
record, UNCALIBRATED:

| Row | Registered | Other arms |
|---|---|---|
| Listing move | 3% (ADR 0083) | 2%, 5% |
| Listing volume ratio | 3 (ADR 0083) | 2, 5 |
| Baseline length, sessions | 5 | 10, 20 |
| Rising run, closes in a row | 3 (`MoverConfig` at `660b37e`) | 1, 2 |
| First-leg window: a run completing later than this after the first listing is extended | none | 15, 60 minutes |
| Largest move above the quiet price (the median close of the 30 minutes before the run): above it a run is extended | none | 10%, 25% |
| Extension rule: an extended run is refused, or admitted after a pullback of 5% from the highest close | refused | the 15-minute and the 10% arm, each with the pullback |

The last three rows are in no code yet. Their definitions above are this record's reading of ADR 0083's
requirement 2; if the strategy's record defines them otherwise, the arms are registered again before any
trial. `MoverConfig` requires a no-chase cap on the move from the reference close and gives it no
default, so `Mover` never runs without one; no value of it is registered, so it is off in every arm.
That is a run `Mover` cannot make, and a departure: an arm is replayed again with a cap once a record
registers its value.

**The method.** The owner supplies a fixed list of mover days, kept outside the repository; this record
names neither its contents nor its origin, and its SHA-256 is entered under Confirmation when it
arrives. Its dates are split in order: the earliest 70% are development, the rest a holdout. Skfolio's
`WalkForward` with an expanding training set walks the development dates in blocks of a fifth of them,
rounded down (four folds where the count divides by five, more where it does not, and dates past the
last whole block judged by no fold); in each fold the arm with the largest share of names entered not
late on the earlier blocks (ties to the larger median share ahead, a name not entered counting zero,
then to the arm listed first) is judged on the next block, at every cost. The arm chosen on
all development days is replayed on the holdout once, at every cost; a second opening is refused, and
a run that fails or finds none of its names leaves it unopened. Nothing is tuned on the holdout. This
choice is not a ranking under ADR 0010 and adopts nothing: it picks which arm is reported on later
days. Once the ledger takes Mover trials, arms are ranked only by ADR 0010's protocol. Until the list arrives, the code is built and tested on generated bars and the day's tape.

**Claims.** A list of known mover days answers "given a mover, was it entered early", not "was it found
among all names": a name's rank in the whole market is not computed, and "listed" means it met the
thresholds. The second question waits for whole-market days from the recorder. Outcomes on a list
chosen for having moved are biased upward and show no edge. Until the ledger takes Mover trials with a
cost model, every figure is a DIAGNOSTIC: labelled, counted, and it changes no row. After that each arm
is a trial, and a row changes only through the gate.

**Limits, stated and not solved.** The half-spread comes from live `sip` quotes of ranked names, since
historical quotes are IEX only; it is given to the code, which takes no default and prints gross figures
without it. The adapter serves no historical trades (`_request_trades` is not overridden), so timing
before a live window is from minute bars, which count only minutes with a trade (ADR 0077). A daily
bar is dated by the New York day of its open, which rests on Alpaca opening daily bars at midnight New
York (UNVERIFIED, as the adapter marks it). Prices are raw: a split between the reference close and the
day is not checked. Pre-market coverage of
old days is checked for each name, not assumed. A delisted name may have no bars (the provider loads
active assets, ADR 0083) and is counted missing. Prices are not rounded to Alpaca's tick; the size, the
free slot and the request budget are not modelled; the after-hours session is outside the window.

### Consequences

* Good: "early" is three numbers and a count, fixed before today's tape or any listed day is read.
* Bad: the entry rule is mirrored from a commit not on `main`, so it can drift until it is imported.
* Bad: fifteen arms on a list of movers can pick the loosest listing; only whole-market days can price
  the entries it would also take on names that did not move.

### Confirmation

* `tests/test_early_entry_study.py` holds each measure and the bar model on generated bars.
* The list's SHA-256, the split's dates as counts, each fold's choice and the one holdout run are added
  here as they happen, with the command and its exit.
* This record is `proposed` until the list arrives and the ledger takes Mover trials with a cost model;
  ADR 0087 (the ranking) and ADR 0088 (the strategy) are numbered and not on `main`, UNVERIFIED here.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records.

* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/backtesting/bar-execution.md
  — "With `False` (the default), each bar uses `Open -> High -> Low -> Close`"; "Without a latency model,
  an order submitted from `on_bar` settles immediately against the book left at bar N's close". Resolved.
* https://github.com/skfolio/skfolio/blob/7721090537634a549008a546c8968cdec0eaf69a/src/skfolio/model_selection/_walk_forward.py
  — `WalkForward`: "In each split, test indices must be higher than the previous ones"; `test_size`,
  `train_size`, `expand_train`. Resolved, and run here on generated dates (the tests); its split of the
  owner's list is UNVERIFIED until the list arrives. The registry's entry for skfolio names the gate's
  three measures only, so this use is added there in the registry's next change.
* `src/canon_trading/mover.py` at commit `660b37e` (a branch, not `main`) — the rising run
  (`run_bars` 3, closes each above the one before), `trail` 0.02, `exit_through` 0.005, `chase_cap`
  0.02, `max_move` required and without a default; "on bars alone the emulator cannot activate a
  trailing stop". Resolved to that commit of this repository.
* `src/canon_trading/movers_stream.py` — `normal`: the mean over sessions of the dollar volume of the
  same clock minutes; a bar's dollar volume as close times volume; `NORMAL_SESSIONS` 5. Resolved to
  this repository's `main` at `a0a0b60`, as are the files below.
* `docs/adr/0083-strategy-focus-extreme-movers-from-upstream-parts.md` — the requirement, the starting
  rows (move 3%, volume ratio 3, trade acceleration 1.5), M3 and its missing delisted names. Resolved.
* `packages/canon-alpaca/src/canon_alpaca/data.py` — a bar's `ts_event` is its open and `ts_init` its
  end; `_request_bars` and `_request_quotes` exist and no `_request_trades`; daily bars opening at
  midnight New York is marked UNVERIFIED there. Resolved.
* `src/canon_trading/bars_to_catalog.py` — a window ending less than one bar interval before now is
  refused, so a session's daily bar loads a day later; a later load of a covered day is written as an
  interval of its own. Resolved.
* `docs/adr/0077-quote-study-ema-cross-and-grid-market-maker-pre-registered.md` — "Minute bars count
  only minutes with a trade". Resolved.
* Second reader (`evidence-verifier`), 2026-11-11: the dropped acceleration row, the arms' wording, the
  latency qualifier and whose arithmetic the exit is, the required no-chase cap, the folds' remainder,
  the costs of the walk-forward, three unmarked facts and the Evidence labels. Applied above.
* `/code-review`, 2026-11-11, on the study's code: the holdout was marked before it was replayed, and
  reported at one cost; the tie-break left out names not entered. Fixed in the code, and worded here.
