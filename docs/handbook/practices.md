# Practices

One line per practice, by move. Each line names the record here that decides it, or says "not yet built"
with the roadmap step that will build it. The requirement behind them is [philosophy.md](philosophy.md);
every number is a row of [the rows table](rows.md). `tests/test_handbook.py` checks that each line names a
record that exists or says "not yet built" with its step. ADR 0087, 0088, 0089, 0091, 0092, 0096, 0100,
0101, 0103, 0106, 0107 and 0110 are `proposed` as this page is written: what a line cites from them is
built and on `main`, and open until the record is accepted.

## Two gates, kept apart

* Adopting a setting: the pick's deflated Sharpe ratio at 0.95 or above, with N counted over every trial in the ledger (ADR 0010, ADR 0031, ADR 0080).
* Merit on paper: a probabilistic Sharpe ratio of 0.95 or more over at least 30 independent round trips in at least 5 sessions, replay correlation 0.9 or more, slippage within 1.5 times the model; it produces a draft, never a promotion; not yet built (roadmap step 8; ADR 0083 states the round trips and the sessions, the other terms are the owner's requirement alone).

## Rank

* One full-market ranking generation each minute, published as the signal `movers-rank`; a missing, stale or invalid input publishes nothing, and no generation, an old one or one that does not parse means no entry (ADR 0087, ADR 0107, ADR 0088).
* Score: the move times the symbol's dollar-volume ratio against its own baseline for the same part of the session, with no price or market-cap floor; the requirement's tie-break, trade acceleration, is unknown on bars and breaks no tie (ADR 0087).
* The move is measured from the latest completed, split-adjusted official regular-session close of a session before the trade date; a previous daily bar's close is not an official close without its own authority, so whether Alpaca's daily close is one stays UNVERIFIED, and the move from the regular open is unknown until a source of the official open is named (ADR 0087).
* The complete universe and every unknown row stay in the generation, the top 20 is a projection only, an unknown is unknown and not zero, and `sip` and `boats` figures stay apart (ADR 0087, ADR 0107).
* A print that does not update price or volume by the tape's rules updates neither measure, its conditions taken from Alpaca's conditions endpoint and not from memory, and a correction, cancel, gap or invalid input invalidates the generation; not yet built for prints: the ranking reads minute bars, a gap leaves it unpublished, and a correction or cancel reaches it only as an updated bar (roadmap step 7; ADR 0087, ADR 0107).
* Two clocks: availability drives what a decision may use, acquisition is provenance, and each ranking record carries its latency labels: publication, receipt and generation from the ranking, entry-eligible and submission from the strategy (ADR 0107, ADR 0088). The receipt label holds the bar's `ts_init`, not a measured arrival, which stays UNVERIFIED (ADR 0087).
* The data feed is named in each subscription and request, with one stream per client and no fallback (ADR 0055).
* One stream per login, held under its own lock, and consumers never open another socket (ADR 0069, ADR 0070).
* Full-market reads run data-only, under a task that refuses every request but GET (ADR 0070).

## Enter

* The first leg is a run of strictly rising one-minute closes, which an equal close or a minute with no trade restarts (ADR 0088); earlier entries are arms of the early-entry studies, registered before any result (ADR 0092, ADR 0096, ADR 0100). The requirement states the leg on trades: a non-decreasing run of eligible trades inside a window, no more than a set move above the quiet price, a lower trade restarting the run, and a missing ATR or VWAP making "extended" unknown, never "not extended"; that form is not yet built (roadmap step 7; ADR 0083, gap 3).
* An entry is a DAY limit at the ask, cancelled at a time limit, never a market order (ADR 0088); repricing it within a chase cap is not yet built (roadmap step 7; ADR 0088's departures).
* An order for the extended or overnight session is a DAY or GTC limit marked through the submit's `extended_hours` param; any other order so marked is denied, and an unmarked order waits for the next regular session (ADR 0038).
* Size on paper: a Mover window's slot figure is the smaller of the account's buying power and cash at startup over the slots, to the cent (`session_window.py:1528-1531`), and every Mover paper window sizes each entry for a planned loss through one call, `sizing.plan_for_loss` (`session_window.py:1630`, `mover.py:3945`), which calls upstream's `FixedRiskSizer` (`sizing.py:195`); the one-share bridge strategy is sized apart from this. The planned loss is the figure's whole shares times what each loses at a 0.02 stop (`PAPER_LOSS_SHARE`, `mover.py:546`, the lead's ruling of 2026-11-12; `share_loss`, `mover.py:768`); the budget is the free balance less the entries working and the buys not yet in an account report, capped at the figure, and the prior minutes' dollar flow bounds the shares (`mover.py:3880-3907`). A wider stop sizes down, a tighter one is held at the figure, and a size that cannot be made is refused by name, never estimated (`mover.py:3939-3973`). ADR 0106 states this sizing; its earlier lines that a paper Mover does not use `FixedRiskSizer` describe the code before it and no longer match `main`. A missing or invalid buying-power read refuses startup (ADR 0088, ADR 0091, ADR 0106). The Mover replay sizes from a named buying power over the slots, with no flow bound (ADR 0101).
* Slots: at most seven held names in every part of a paper session, a run that asks for more being refused, each name under its own share cap and position monitor, taken from the published ranking best first (ADR 0088, ADR 0091, ADR 0106). A share of the slots reserved for the newest movers is not yet built (roadmap step 7; ADR 0088's departures), nor is a candidate pool of three times the quota, which no record states (roadmap step 7).
* No order goes into a halt; not yet built (roadmap step 7; ADR 0088's departures; ADR 0083, gap 4).

## Protect

* The protection is the engine's emulated trailing stop-limit sell (`mover.py:43`; ADR 0088). Its first trigger is the planned distance under the average fill, rounded up onto the step (`fill_trigger`, `mover.py:758`), and it then follows the highest bid up, never down. A Mover window that names no trail and no arm runs arm OWN1 (`session_window.py:532`, `:540`; ADR 0106): the distance is one 14-bar ATR of one-minute bars over the price, raised to two quoted spreads when that is larger, and kept over 0.02 when it is larger still, for the sizing above to carry (`mover.py:4321-4341`); sizing refuses one over 0.10 (`sizing.py:45`). `MoverConfig`'s own default, and arm F2's, is the fixed 0.02 (row `TRAIL`). Arming only on a completed confirmation above entry plus round-trip cost plus the noise floor, with an unknown cost basis or quote leaving it unarmed, is not yet built, and the requirement alone states it (roadmap step 7).
* The floor under that distance is two quoted spreads wherever a usable spread is quoted (ADR 0106). Two cases fall under it in the code on `main`, both read true by a review of pull request 338 and open until their fix lands: where the stop crosses one dollar, the distance rounded down onto the entry's step can fall under the floor without a refusal, only a distance of nothing being refused (`mover.py:3967-3973`); and an opening entry with a cold ATR takes the fixed 0.02 and records the floor without applying it (`mover.py:4245-4258`, known limit 2 at `mover.py:535-536`), a case no paper window reaches today, since a window refuses any opening arm but off (`session_window.py:812-818`). Outside the opening, a cold-ATR entry takes the fixed 0.02, raised to the floor when the floor is larger under the `own` cap of arm OWN1 (`mover.py:4287-4306`).
* Warm start: under any trail but the fixed one, a slot's ATR warms when the slot opens, first from the cached one-minute bars of the session part now open (`_warm_atr`, `mover.py:2913`), then, if those leave it short, from one bars request of the last hour, not bounded to the part, ending 15 minutes ago on the overnight feed (`mover.py:1368-1375`, `:2700-2701`). At most 20 such requests go out a minute (`mover.py:490`). A slot still short of 14 bars, or whose request is refused or unanswered, enters on the cold path, and the window record names each outcome under `warm_up`. The cold path is ADR 0106's; the warm-up itself is the lead's ruling of 2026-11-12 and pull request 340, and no record holds it yet.
* Every holding in gain has protection that follows the gain; not yet built (roadmap step 7; ADR 0088's departures, the gain lock).
* A breach needs a count of observations over a span, never one quote, print or bar; not yet built: the emulator releases on the first quote at the trigger (roadmap step 7; ADR 0088's departures).
* The sale is a limit the emulator releases, sent again every chase interval down to a floor one chase cap under its first price, each re-price a cancel and then a new limit, since no order is modified (ADR 0088). The requirement's fast sell, one managed limit at the better of the bid and the last price less the chase cap, repriced at most once per interval and serialised, an acknowledgement not being a fill, is not yet built in that form, and the requirement alone states it (roadmap step 7).
* A broker stop is never sent in extended hours, so it can only be a backstop for an outage in the regular session (ADR 0036, ADR 0083).
* The backstop, a wide stop the broker holds for an outage, is raised, never lowered; not yet built (roadmap step 7; ADR 0088's departures).

## Re-enter

* Flat is read on Alpaca's word: no open order, no position, and every request of the window ended (ADR 0060).
* A re-entry waits for the sale and a cooldown (ADR 0088, ADR 0091).
* After a sale the slot goes to a reclaim of the same name or to another ranked name of the window's roster (ADR 0088, ADR 0091); the requirement's retry band and rotation band are not yet built, and the requirement alone states them (roadmap step 7).
* A reclaim is bought on a rising run that closes above the sale's price, after the cooldown (ADR 0088); the requirement's pullback, held higher low and hold, never the first print, is not yet built (roadmap step 7; ADR 0088's departures).
* Rotate out of a held name only when the expected lift exceeds twice the round-trip cost plus a margin; not yet built, and the requirement alone states it (roadmap step 7).
* On paper, re-entry is unbounded until the window winds down (ADR 0091).

## Custody and budgets

* An order the owner places is never touched; one writer per account and one exit per quantity, and anything foreign freezes its symbol and reports. The close by hand keeps to that: it acts only on what our client order IDs wrote and freezes and names a symbol with anything else in it (ADR 0060; `canon_trading.custody`). The account check after a window does not yet: not yet built there, and no record decides it (roadmap step 6; ADR 0060; see philosophy.md).
* One account per lane: account A for the paper tests, account B for the session windows, each under its own lock (ADR 0069).
* The node's own throttle: 50 submits and 25 modifies a minute against Alpaca's 200 calls, 180 a minute for both on paper nodes, and a notional cap per order (ADR 0049, ADR 0106).
* Pace every action with the priority cancel, reduce, modify, query, submit, reserve capacity for protection, and back off on a 429; not yet built, and ADR 0083 refuses a pacer written here, so it needs a record of its own naming the upstream part (roadmap step 7). What is built: entries yield above a soft line of requests in 60 seconds, and a window fails on a rolling peak at the ceiling or on any 429 (ADR 0088, ADR 0106).
* Use about 80 per cent of the account's action budget over a session, protection first, and investigate a session with no action for fifteen minutes, never answering it by forcing a trade; not yet built (roadmap step 7).
* Every session but the live lane trades on paper alone; the live lane places live orders only through its own tasks, nothing trades live until its mechanism is accepted, and no session reads a credential (ADR 0009, ADR 0103).

## Sessions

* Alpaca's clock and calendar are read before anything is placed, and each session's bounds come from the calendar day (ADR 0044).
* Paper session windows run on a schedule whenever a session is open (ADR 0050).
* Paper runs round the clock on weekdays with no conservative profile: the full factor stack, every factor set as a paired arm with and without it, and paper profit and loss alone deciding nothing; not yet built (roadmap step 6).
* Simulation runs on Alpaca's live data through NautilusTrader's sandbox, with no order sent to Alpaca (ADR 0070).
* Ask which clock a number came from: a historical minute bar ends a minute after its start, a daily bar at the next New York midnight, a live bar at receipt; not yet built (roadmap step 7).

## Evidence and learning

* One cumulative trial ledger, every trial kept and counted, with one writer at a time (ADR 0035, ADR 0054, ADR 0081).
* Tiers are never pooled, history is never reset and rejected variants are kept; a paper run is not a trial (ADR 0010, ADR 0083).
* A trial's identity is the data it read and the configuration it ran (ADR 0041, ADR 0053).
* A backtest is read against upstream's simulated venue, with its report saved beside each study (ADR 0067).
* The probability of backtest overfitting is reported beside the gate, never a pass or fail (ADR 0027, ADR 0032).
* Adoption needs a result above the incumbent and above holding flat at doubled costs, otherwise the incumbent stays and the negative is recorded; not yet built (roadmap step 7; ADR 0083's adoption test).
* An arm, a base or a clarification is registered only when its text is hashed with SHA-256 and that hash is written down, with its New York time, in a tracked file or in the dated digest kept outside the repository, in the same step; text that exists only in a working tree is a draft and registers nothing. This is the lead's standing rule of 2026-11-12; no record holds it yet and nothing checks it mechanically. The records that register arms before any result is read are ADR 0092, ADR 0096 and ADR 0100.
* A builder starts from `main`, or from a predecessor that is already in review on a branch of its own. Work is not carried into another lane's tree unless it has a landing branch of its own. A unit lands as soon as its review and its paper window are done. A tree composed to fire a run is fired and thrown away, and work never lives in it. One change per worktree, each resolved cleanly against `main`. One branch per change and a worktree for each parallel session are ADR 0014's; the rest is the lead's standing rule of 2026-11-12, which no record holds yet and nothing checks mechanically.
* Negative results are kept as records (ADR 0052).
* Every exit leaves its counterfactuals at one times and stressed costs; not yet built (roadmap step 7).
* Every session ends with a reflection written to [the journal](journal.md); not yet built (roadmap step 6).
* The nightly study runs each plan's registered rows again on the recorded data, ranks them through the gate and writes the night's dated entry, which a session lands in [nights.md](nights.md); no paper rule changes from paper profit and loss (ADR 0089).
* A selected history of events is not a ranking population, and a day without a recorded event is not a negative, and selected losers are not ordinary controls; not yet built (roadmap step 7; ADR 0083, M3).
* The trading path needs a failing test first and a paper or simulation run; off it, converged documentation and one review land a record (ADR 0074).
