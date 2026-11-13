---
status: proposed
date: 2026-11-13
decision-makers: repo owner (requirement, 2026-11-11: every session ends with a reflection against holding flat and the movers missed); lead orchestrator (Claude) for the rule that a baseline always runs beside a rule (2026-11-13); master orchestrator (Claude) for the definitions, under the owner's delegation (ADR 0074)
review-by: 2026-12-13
---

# Each rule is measured against holding flat with its exit count, beside a baseline, its shadow arms and the movers missed, on one trade date; open: no upstream ships a do-nothing baseline, a shadow evaluation or a missed-mover review

## Context and Problem Statement

The handbook asks each session's reflection for what each rule did against holding flat, with its exit
count, each exit's counterfactuals and the movers missed (`docs/handbook/journal.md`, "The session-end
reflection"; `docs/handbook/philosophy.md`, "How we learn"). None of those measures is defined: not the
session's boundary, not what a counterfactual holds fixed, not which gainers list counts as the day's, and
not the tape and costs a figure is computed on. The first reflection, on 2026-11-13, had to choose each one
by hand. A measure chosen at the time of reading can be chosen to flatter a rule; this record fixes them first.

## Decision Drivers

* A rule is judged against doing nothing, never by its own profit and loss alone (ADR 0083's adoption test).
* Every figure carries the count it stands on, so one session's anecdote reads as one session (philosophy,
  "What we refuse").
* Measures are defined before anything that computes them is built, and each input is a tape or a record
  this repository already keeps.
* Public repository: a figure in a tracked file is a count, a sign or a return per unit of notional, never an
  account's money (this record's choice).

## Considered Options

1. Live against a replay of the same session only, as QuantConnect reconciles a live deployment against an
   out-of-sample backtest run beside it.
2. Results broken down by entry and exit reason only, as Freqtrade groups profit by `enter_tag` and
   `exit_tag`, in its backtest analysis and in its live bot's reports.
3. Define the measures here, taking the parallel run from 1 and the per-reason attribution from 2, and add
   what neither ships: a do-nothing baseline, shadow arms and the movers missed.
4. Leave them to each reflection's writer.

## Decision Outcome

Chosen: **option 3**. Options 1 and 2 are the nearest shipped practice, and each covers one part; option
4 is what the first reflection had, and is what this replaces.

**The gap (AGENTS.md rule 3).** No upstream read ships a do-nothing (flat) baseline per rule, a shadow
evaluation of rules not taken, or a review of the movers a system passed over. The nearest sources are
QuantConnect's reconciliation, which runs one out-of-sample backtest beside each live deployment and compares
the two equity curves, and Freqtrade, which groups profit by entry and exit tag (its `backtesting-analysis`
on backtests, and its live bot's per-tag reports). Nearer still, but not these: Freqtrade's backtest reports
a "Market change" (an average buy-and-hold change across its pairs) and counts and lists rejected entry
signals; NautilusTrader's tearsheet takes optional benchmark returns to overlay. Buy-and-hold is not holding
flat, a rejected signal is not a rule replayed, and an overlay is not a figure per rule. None of these is a pin
here, and none is adopted by this record: they are the shape, cited, not a dependency.

**The rule (the lead, 2026-11-13).** A baseline always runs beside a rule, and every figure is stated against
holding flat, per rule, with its exit count. Holding flat is cash at zero return: it is the baseline, not a
trial (ADR 0089, step 4).

**The measures.**

* **Session and trade date.** A session is one New York trade date on which Alpaca's calendar has a session:
  from 20:00 on the evening before to 20:00 on the date, overnight, pre-market, regular and after-hours, as
  Alpaca's 24/5 page dates an execution from 20:00 to the next date; its overnight session follows the NYSE
  holiday calendar. A weekday with no session has no reflection. An entry, all its fills and the sells that
  close it are one exit, dated by the trade date of its last fill; an exit whose entry was on an earlier
  trade date counts on the exit's date, and the reflection names it so.
* **Against holding flat, per rule.** An exit's return is the quantity-weighted average price of the sells
  that closed it over that of its buys, less one, in basis points. The fills are real, so the spread they
  crossed is in their prices already; the costs then taken off are the regulatory fees on its sells and buys
  (FINRA's trading activity fee and the consolidated audit trail fee) and any commission (`UNVERIFIED` that
  it is zero on these orders; paper charges neither). At doubled costs, each fill is also moved a further
  half of its quoted spread against it, and the fees are doubled. A rule's result is the count of its exits
  and the sum and median of their returns, at one times and at doubled costs. The rule is the exit's reason
  (the trailing stop, the wind-down, the unpriced exit) and the entry's arm; both are kept, as Freqtrade
  keeps both tags. Holding flat is zero for every exit, so the figure against it is the return itself; a
  figure with no exit count is not stated.
* **Each exit's counterfactuals,** from the same entry fills, on the window's own tape, priced as below:
  * *held to now*: the position held from its entry to the window's end, its wind-down's start, and sold
    there; a window is at most 60 minutes (ADR 0044), so "now" is the window's end, never the session's;
  * *matched flat*: no entry, zero, for the same span;
  * *managed*: the exit that happened;
  * *managed with re-entry*: the managed exit, then the next entry the window's own re-entry arm would have
    taken on the tape, and its exit, replayed; a window with no re-entry arm has none;
  * *shadow arms*: each registered trail arm (`TRAIL_MODE` and the arms in `docs/handbook/rows.md`)
    replayed from the same entry fills with its own exit; a shadow arm sends nothing.
  Each is reported with the count of exits it stands on, at one times and at doubled costs.
* **Pricing a counterfactual.** A counterfactual sale is at the last two-sided bid at or before its time;
  if there is none in the 60 seconds before it, the sale is unpriced, counted as unpriced and given no
  return, never filled at a guess. The costs are those above, plus the half spread the bid already implies.
* **Missed.** The day's gainers are the gainers list (gainers only, not losers) of Alpaca's movers endpoint
  in the recorder's last poll before 20:00 New York (ADR 0070). Alpaca computes a mover's change "from the
  previous closing price and the latest closing price", and for stocks the list "resets at market open";
  whether pre-market and after-hours trades move it is `UNVERIFIED` (the recorder's header). A gainer is
  missed when no entry of ours filled on it on any paper account in that session's windows; shadow arms do
  not count. Each is counted with the reason the ranking or the entry gave, against the size of the list,
  and a gainer the ranking never named is counted apart from one it named and passed over.
* **The tape.** Quotes and trades exist only inside a window: the window keeps those that reached its
  strategy on the feed of its part (ADR 0044's feed row), and the recorder keeps only the movers and
  most-actives lists, no quotes. So every counterfactual ends at its window's end, and one that would run
  past it is not priced. The fills are Alpaca's for our orders; the account read prints the trade date's
  ended orders by the same 20:00 bounds (`src/canon_trading/custody.py`, landing beside this record).

Nothing of the baseline, the shadow arms or the missed count is built until this record is on `main` with
its one evidence-verifier pass. The rows for the counts (how many exits make a figure worth a change) are
ADR 0083's floor and stay UNCALIBRATED here.

### Consequences

* Good: every reflection states the same figures on the same bounds, so sessions compound and can be summed.
* Good: a rule that wins only by its own profit and loss, and not against holding flat at doubled costs,
  shows as such.
* Bad: the shadow arms and held-to-now need a replay from each entry fill, which does not exist yet.
* Bad: a gainers list read once before 20:00 misses a mover that rose and fell inside the day.
* Bad: with no quote tape outside the windows, held to now means held to the window's end, and a shadow arm
  that would hold longer is unpriced.
* Bad: returns in basis points hide the size of a holding; the size is the sizing rows' (ADR 0083).

### Confirmation

* A test on a shaped session computes each measure: trade-date bounds at 20:00 on both sides, an exit from
  an earlier date's entry, an exit closed by two sells, its return at one times and doubled, held to now and a
  shadow arm on the same tape, an unpriced sale, and a missed count.
* The next reflection after the build states each figure with its exit count and the baseline beside it.
* The record stays `proposed` until the measures are computed by code, not by hand, on one paper session.

## Evidence

Read 2026-11-13.

* https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/reconciliation — "we run an
  out-of-sample (OSS) backtest in parallel to all of your live trading deployments"; "If your algorithm is
  perfectly reconciled, it has an exact overlap between its live and OOS backtest equity curves" —
  `fetched`, and checked word for word by the evidence-verifier.
* https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/advanced-backtesting.md — profit
  "grouped by enter_tag and exit_tag"; the analysis "is only available for backtesting, *not hyperopt*";
  `--rejected-signals` — `fetched` by the evidence-verifier.
* https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/telegram-usage.md — the live bot's
  `/entries`, `/exits` and `/mix_tags` reports by tag — `fetched` by the evidence-verifier.
* https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/backtesting.md — "Market change" and
  the rejected entry signals count — `fetched` by the evidence-verifier.
* https://nautilustrader.io/docs/md/latest/concepts/visualization.md — the tearsheet's optional
  `benchmark_returns` — `fetched` by the evidence-verifier.
* https://docs.alpaca.markets/us/reference/movers-1.md — the change "from the previous closing price and the
  latest closing price"; "resets at market open" — `fetched` by the evidence-verifier.
* https://docs.alpaca.markets/us/docs/regulatory-fees.md and https://docs.alpaca.markets/us/docs/paper-trading.md
  — the regulatory fees on sells and on trades; paper does not charge them — `fetched` by the evidence-verifier;
  no page read states that equities on this API carry no commission, so that stays `UNVERIFIED`.
* The absence of a flat baseline per rule, a shadow evaluation and a missed-mover review in the maintained
  frameworks read: NautilusTrader's reports, backtesting and visualization pages, Alpaca's activities and
  portfolio history, QuantConnect and Freqtrade above — a search of what was read, not proof of absence;
  `UNVERIFIED` beyond it.
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api — the overnight session from 20:00 New
  York belongs to the next trade date — `fetched` 2026-11-13 for the account read.
* `docs/handbook/journal.md` and `docs/handbook/philosophy.md` — the reflection's required contents, read in
  the tree; ADR 0083 — the adoption test and doubled costs; ADR 0089 — holding flat as zero, not a trial;
  ADR 0070 and `src/canon_trading/movers_screener.py` — the movers recorder, what it logs, and its
  `UNVERIFIED` on extended-hours trades; ADR 0044 — the feed row and the 60-minute window.
* The first reflection's by-hand counts, 2026-11-13 — kept outside the repository; they show the measures
  had to be chosen at reading time.
