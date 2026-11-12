# Philosophy

This page states the requirement: why the trading work here is done the way it is. A record in
`docs/adr/` states each decision with its sources. Where pinned upstream evidence or a measurement made
here contradicts a requirement on this page, the record follows the evidence and says so, and the lead
tells the owner. What follows is the owner's requirement of 2026-11-11, restated; it is an instruction,
not evidence, and every number in it is a row of [the rows table](rows.md), UNCALIBRATED until a
measurement here calibrates it. A record cited beside a sentence holds the part that sentence names, no
more; the rest is the requirement alone, with no record behind it yet. ADR 0083 restates the part of it
that sets the strategy focus.

The practices that carry it out are listed in [practices.md](practices.md), each with its record or
marked not yet built.

## The four moves

The strategy work here concerns extreme movers only (the owner's decision of 2026-11-11, ADR 0083).

1. **Rank.** Trade only the most promising movers, found as they start, across the whole market. The
   score is the move times unusual volume against the symbol's own normal; raw dollar volume, share
   count, price and trade rate never form the score. A missing, stale or invalid ranking means no
   entry, with no fallback. No upstream ships the score; ADR 0087 (proposed) decides its formula, the
   move times the ratio of dollar volume to the symbol's own baseline for the same part of the session.
2. **Enter early.** Enter the first rising run of the first leg. A late, extended move is entered only
   on a pullback and reclaim, never at a fresh spike high. Size comes from planned risk, not conviction
   (ADR 0083 names upstream's `FixedRiskSizer` for the sizing; a paper window is sized from the
   account's buying power over its slots instead, ADR 0106).
3. **Protect.** A sell trigger sits just below the entry, outside the tape's noise, and only ever rises
   with the high. Once the gain covers its costs, a gain lock holds it. A breach must persist: no exit
   fires on one quote, one print or one bar. The fast sell then chases within a cap. A wide broker stop
   is only the backstop for an outage (ADR 0083 records that Alpaca's trailing stop does not trigger
   outside the regular session).
4. **Re-enter.** After every fast sell and a cooldown, buy back on a reclaim while the name still ranks,
   or rotate into the new top mover. Missing the rest of a move is a loss too (ADR 0083 places
   re-entry and rotation in its step M2).

The aim is small, bounded, planned losses and larger gains, not zero losses. The order rate is a budget
to use, within the broker's limits and through the pacing, with protection first (ADR 0049 sets the
node's share of Alpaca's limit, and ADR 0106 the paper node's).

## What never changes

* **Custody.** An order the owner places is never cancelled, sold back, resized or adopted by an agent;
  the owner's fill is recognised at once and protected. One writer per account and one exit per
  quantity; a foreign order or a quantity that does not match freezes that symbol and reports.
  Protective actions come first in every budget. Where today's code departs: the session window's
  account check after the stop cancels every open order Alpaca lists for the window's symbol and closes
  the quantity held, whoever wrote them (ADR 0060; `canon_trading.session_window`). Custody requires it
  to act only on orders and quantity its own client order IDs wrote, and to freeze and report anything
  else. That correction is not yet built, and no record decides it yet; ADR 0083 refuses a custody layer
  written here, so the correction stays within the window's own account check.
* **Paper is the proving ground and is never gated.** Profit and loss, loss budgets and counts are
  recorded with every action and never block. The hard lines on paper are custody, one writer, the
  pacing, the broker's own limits, the owner's halt, nothing live, no secrets, and money in the engine's
  types. Records hold three of them: nothing live outside the live lane and no secrets (ADR 0009,
  ADR 0103), the node's share of the broker's limit (ADR 0049), and one lock per paper account (ADR 0069).
* **Live goes through one lane, by the owner's authority.** Every session but the live lane works on
  paper only (ADR 0009). The owner authorised one live lane on 2026-11-12, a protector first: it places
  live orders only through its own tasks, and nothing trades live until its mechanism is accepted and on
  `main` (ADR 0103, ADR 0110, both proposed). The requirement adds that live moves only through
  evidence-gated promotion within limits the owner approves.
* **Long only.** Short selling and options trading need a further decision; none is recorded (ADR 0083).

## How we learn

* **Every rule comes from the tape**, in this order: capture the incident's exact trades and quotes;
  write a replay test that fails first; write the rule so the test passes; write the record, the row
  and the handbook line; record a paper run against holding flat. No rule on opinion. The trading path
  already needs a failing test first and a paper or simulation run (ADR 0074).
* **Numbers are calibrated, not argued.** Every threshold is a row in [the rows table](rows.md) with a
  source. The requirement asks that a row change between sessions only, when a walk-forward replay beats
  both the incumbent and holding flat at stressed costs, beyond its measured noise. The records here
  differ, and they govern: ADR 0083's adoption test is a result above the incumbent and above holding
  flat at doubled costs with a deflated Sharpe ratio of 0.95 or above, and ADR 0010 chose that ratio
  over a walk-forward series of hold-outs, since a hold-out ignores how many trials were run.
* **Compounding by self-reflection.** Every session ends with a reflection: what each rule did against
  holding flat, and which movers were missed. Every exit leaves its counterfactuals and a lesson, in
  [the journal](journal.md). The nightly replay writes proposals only, in [nights.md](nights.md);
  adoption is the rule above, as a separate change reviewed before it lands (ADR 0017).
* **The evaluation loop never idles.** Simulation and paper run beside an open session on live data;
  replay runs when markets are closed (stress, reflection, proposals). Outside regular hours every tier
  sends limit orders only; the adapter sends an order into the extended or overnight session only as a
  marked DAY or GTC limit, and an unmarked one waits for the next regular session (ADR 0038). The loop produces evidence and
  proposals only. The lanes it runs in are ADR 0068's and ADR 0070's.

## What we refuse

Churn; fake protection; hindsight tuning; live leakage (ADR 0009); a charting vendor's data as an input
to a machine decision (display only); one-day anecdotes as grounds for a change; short selling or options
trading without a further decision (ADR 0083). Negative results are kept (ADR 0052 keeps one), and
outcomes are read only as of their cutoff. Every trial stays in the ledger (ADR 0010).

## What is still unknown

Whether early entry, a tight trigger and re-entry beat holding flat net of costs. Confidence in the
direction rests on the owner's statements; confidence in the economics is low. The rules exist to
measure that, and ADR 0083's step M3 is the study that does.

## The lanes, by purpose

| Lane | Purpose | Record |
|---|---|---|
| PAPER | Prove the stack and each rule on Alpaca's paper accounts, round the clock on weekdays: account A for the paper tests, account B for the session windows | ADR 0044, ADR 0050, ADR 0069 |
| SIM | NautilusTrader's sandbox execution client matching locally on Alpaca's live data, with no order sent to Alpaca | ADR 0068, ADR 0070 |
| REPLAY | Backtests recorded as trials and ranked through the gate, every day of the week | ADR 0010, ADR 0067, ADR 0083 (M3) |
| RESEARCH | Data-only probes that cannot trade, and sourced research for a record | ADR 0070, ADR 0083 (M0) |
| LIVE | One live lane, by the owner's authority, a protector first; every other session never places, modifies or cancels a live order, and nothing is live until the lane's mechanism is accepted | ADR 0009, ADR 0103, ADR 0110 |

Tiers are never pooled: a paper run is not a trial, and nothing it earns enters a ranking (ADR 0083).

## One practice, one home

Authority runs: this page (why), [the architecture page](../architecture.md) (the trade path), the
records (decisions with sources), [the rows table](rows.md) (each setting, its source, its proof),
`AGENTS.md` (short operating rules that link and never restate), then settings and scripts (the rule as
mechanism). A rule stated in two places is a defect. A new session needs only the repository.
