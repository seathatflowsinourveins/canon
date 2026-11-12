---
status: proposed
date: 2026-11-11
decision-makers: coordinator, under the lead's delegation; master orchestrator (Claude) records; repo owner ratifies
review-by: 2026-12-11
---

# A bars-only Mover replay for tuning, with named sizing and uncalibrated costs; quote parity, cost calibration and live sizing open

## Context and Problem Statement

The learning loop's tuning study (N2, `canon_trading.tuning_study`) ranks Mover settings on recorded SIP
minute and day bars, and on BOATS bars overnight, before any of them reaches a paper window. No adopted upstream
supplies a bars-only replay of the Mover, a buying-power allocation across slots, or a calibrated cost model.
The live Mover is driven by quotes and NautilusTrader's emulator. The study needs a stated experiment, so
that a ranked key names what was replayed and the replay is not taken for the live strategy.

## Decision Drivers

* Every trial goes through the existing ledger and `ranking.rank` (ADR 0010, ADR 0035). A key must name
  the execution and sizing that produced its returns.
* No fabricated quotes or prints. Only recorded bars are used.
* ADR 0092's registered listing rules, folds and one holdout opening are reused, not re-derived.
* A promotion must carry the whole ranked configuration or be refused (N2/N3 contract).

## Considered Options

1. Replay the live `Mover` unchanged on bars. It needs quotes for its chase and its emulated trailing stop,
   so on bars it either never trades or needs fabricated quotes.
2. A named bars-only subclass, `BarsReplayMover`, executing on NautilusTrader's bar path with native
   trailing stop-limit orders, sized by a named buying power over slots, with zero fees named
   `zero-fees-bar-close` and marked uncalibrated.
3. Wait for quote and trade recordings to replay the live strategy exactly.
4. Drive the search through Optuna's `GridSampler`, which is exhaustive and stops when the grid is spent,
   or register each replay as an already-evaluated trial with `Study.add_trial` and `create_trial`.
   Considered, not taken: the search keeps `RandomSampler(seed)` with `ask` and `tell` in a sampler
   study of its own, and the ledger records each replay itself (ADR 0010), since Optuna documents no
   recipe for a study used purely as a ledger of externally evaluated trials (scout, 2026-11-12).

## Decision Outcome

Chosen: **option 2**, proposed, for simulation only.

* **Execution.**
  * Bars of one timestamp are taken together after delivery.
  * Entries settle at a completed bar's close.
  * Native trailing stop-limit orders, given explicit starting prices, match on NautilusTrader's fixed
    synthetic Open, High, Low, Close path. Adaptive high/low ordering is not set, and neither path
    reconstructs the real trade sequence.
  * A callback 1 ns after an entry places its protection before the next bar.
  * A native stop-limit's price is at least one tick, since the close it is seeded from can lie under a
    sub-dollar entry's limit offset.
  * The pinned engine logs and suppresses an exception raised in a Python timer callback, so the
    replay keeps the first one its handlers raise, handles nothing after it, and raises it once the
    engine stops: a broken replay fails its trial rather than ranking as a run that never traded.
  * The timer ordering and the native trailing activation rest on the builder's engine line
    references (Evidence), which were spot-checked only: UNVERIFIED.
  * Known calendar bounds close each session against the last known book.
* **Sizing.** Each entry is the named buying power divided by slots, in whole shares, capped by free cash and
  by any named share cap. `FixedRiskSizer` is never called, and the live flow-share cap is not applied,
  although `flow_share` stays in the key. This is the coordinator's study rule, not an
  upstream formula.
* **Costs.** Costs are `zero-fees-bar-close`, with maker and taker rates of zero, marked **UNCALIBRATED**.
  No spread, latency or slippage is modelled.
* **Identity.** The execution model, its settlement delay, the sizing inputs and the cost name are part of
  every Mover trial's key.
* **Reuse.** `early_entry_study`'s `_listing`, `folds`, `split_dates` and `open_holdout` are imported. Each
  held date is opened once, by a replay that succeeds, under the default ledger's `holdouts` folder, which
  the early-entry study shares whatever ledger a study writes. A subset, superset or overlap of dates
  already opened is refused, as is a list whose old marker sits beside it. A registration's held dates
  must be the plan's own last 30%. An opening given its arms writes a manifest of them, each with its
  registration commit, and may list an arm as "registered, replay pending"; that arm alone may replay on
  those dates, once, at that commit (ADR 0100). The tuning study's opening lists its bars arms as
  replayed and its waiting arms as pending, each at the checkout's HEAD when it opens. The manifest is
  written after the date markers and replaced whole, so the manifest never blocks the next attempt; a
  date marker written before another failed still marks its date opened. A holdout to which arms are added after its opening is
  this repository's own rule: no finance source found prescribes one (scout, 2026-11-12).
* **Samples.** A slice's sample ends at its last bar when that comes before the calendar bound, so a run's
  end is booked on a day holding a bar (after-hours in EST ends at 01:00 UTC). A name with no 1-DAY bar
  before its last session never has a reference close, so its part is named as no data.

Open, and what would close each:
* **Quote and emulator parity:** needs quote recordings and a replay of the live `Mover` on them.
* **Cost and spread calibration:** needs paper fills compared with replays (the nightly loop's fill reading).
* **Sizing:** the live window's sizing differs, so a promotion is refused until it matches.
* **Multiple testing beyond DSR:** `arch` ships `SPA` (Hansen) and `StepM` (Romano-Wolf), a later
  candidate through `adopt-upstream`. Clustering a trial's event outcomes by day, one return a day from
  NautilusTrader's daily series for `ranking.rank`, is this repository's own rule; no source found treats
  the day as the cluster unit (scout, 2026-11-12).

Limits recorded at review (2026-11-11):
* Each trial prepares its listing frames again, and the frames scan the whole history: the cost grows with
  the square of history length and with the trial count.
* The part bounds are written in more than one place (`session_of`, `bounds`, `BarsReplayMover._window`,
  `tuning_contract`), so a change to one part's hours must be made in all of them.
* After-hours in EST spans a UTC midnight, so one New York session's returns fall on two UTC days of
  NautilusTrader's daily series.

Limits recorded at the second review (2026-11-12):
* The ledger keys a top-level datetime field by its ISO text, outside the canonical form's own
  function: the same instant in two time zones keys twice, and a datetime nested in a field is refused.
* `opened_holdout` called with no dates reads the list with a parser of its own and, on a list it cannot
  read, checks only the old marker beside it. Every caller in `src/` passes the dates.
* Each trial still rebuilds its listing frames over the whole history (above). `ReplayData.coverage`
  has no reader, and the module's `rolling_flow`, `read_bar_metrics` and `BarMetrics` have no production
  caller until the VWAP sidecar lands.

### Consequences

* Good: a ranked key says exactly what it measured, and the gate stays `ranking.rank`.
* Good: the subclass goes away once the Mover takes compatible sizing and a quote replay exists.
* Bad: a DSR earned on bars says nothing yet about quote-driven fills, spreads or halts.
* Bad: with zero costs, a ranking favours high-turnover settings until costs are calibrated.
* Neutral: the replay and its sizing land with this record still `proposed`. AGENTS.md rule 3 says to stop
  at a proposed record when no upstream covers a need; the coordinator's delegation (the front matter)
  takes them further for simulation only, as ADRs 0087, 0088 and 0091 did for code that is also still
  proposed. Nothing here reaches an order path, and the record is accepted or superseded when the owner
  ratifies it.

### Confirmation

* `tests/test_tuning_study.py` replays generated SIP and BOATS catalogs through the real engine, ledger and
  rank, and checks that the key names the execution and sizing and marks the cost UNCALIBRATED. The cost
  name and the zero rates are refused when changed (`ledger._mover_trial_config`); the 1 ns delay is
  fixed in the key and the strategy, with no input to change; no test asserts them in the key.
* `tests/test_early_entry_study.py` checks that a holdout opening is used up only after success, and that
  overlapping dates and an old marker beside a list are refused.
* `tests/test_early_entry_study.py` checks that an arm pending at an opening replays once, at its
  commit, and that every other replay on those dates is refused; and that a manifest arm with no commit
  or an unknown status is refused.
* `tests/test_tuning_study.py` checks that an error inside a replay handler fails the replay, and that a
  native stop-limit seeded under its limit offset is priced at least one tick.

## Evidence

Read 2026-11-11.

* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/backtesting/bar-execution.md
  (read in the pinned copy, `~/.cache/canon/upstream-src/nautilus-trader@d53849b216e2`),
  lines 51-60 (four synthetic updates per bar, orders matched after each, and an order from `on_bar` acting
  only after all four), 68-78 (the fixed Open, High, Low, Close path by default; at line 75 the adaptive
  ordering is "a deterministic heuristic, not a reconstruction") and 96-106 (a venue with an explicit `MakerTakerFeeModel` at zero rates):
  `resolved` at the pin, read this session.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/python/nautilus_trader/backtest/__init__.pyi
  lines 445-469: `add_venue` takes `fee_model` and
  `bar_execution` (default true). `resolved`, read this session.
* `src/canon_trading/early_entry_study.py` at this change: `MAX_BASELINE` (line 98), `_listing` (322),
  `split_dates` (576), `folds` (589) and `open_holdout` (715) are the helpers imported. Read this session.
* Same pin, `crates/common/src/timer.rs` lines 203-214: `PythonTimeEventCallback::call` "Logs and
  suppresses any exception raised by the callback." Read 2026-11-12 in the pinned copy.
* The `upstream-scout`'s study-practice landscape (2026-11-12, saved by the lead): Optuna's `GridSampler`,
  `add_trial`, `create_trial` and `enqueue_trial` from its docs, fetched; `arch`'s `SPA`, `StepM` and `MCS`
  from https://arch.readthedocs.io/; no source found for a holdout with arms added later or for clustering
  by day. The primary multiple-testing papers could not be read: UNVERIFIED.
* Same pin, `crates/backtest/src/engine.rs` lines 944-950: timers are flushed after a timestamp's data;
  lines 2097-2106, inside `settle_venues`: the venues settle and match after the handlers. Spot-checked by
  the evidence-verifier.
* No landscape comparison was run for "no adopted upstream supplies a bars-only Mover replay": UNVERIFIED.
* NautilusTrader v2.0.0rc9 (after this pin) changes `trailing_stop_calculate*` errors and fixes reservations
  after rejected trailing stop-limit orders, and fixes backtest timer cancellation across data gaps and
  tail flushes, which bears on the replay's timer-driven session closes, per its release notes. Moving
  the pin re-checks this record.
* The builder's report, `cx-n2-tuning/REPORT.md` (2026-11-11), gives further pinned engine, risk and matching
  line references for settlement and native trailing activation. They were not re-read here: UNVERIFIED in
  this record.
