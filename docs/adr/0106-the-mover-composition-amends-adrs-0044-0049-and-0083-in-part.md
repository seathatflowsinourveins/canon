---
status: proposed
date: 2026-11-12
decision-makers: the lead for each dated decision below (2026-11-11 and 2026-11-12), under the owner's decision of 2026-11-11 that paper carries no risk limit; master orchestrator (Claude) for the record, under the owner's delegation (ADR 0074)
review-by: 2026-12-12
---

# The Mover composition amends three accepted records in part: a 60-second gap between windows (ADR 0044), the paper throttle at 180 a minute (ADR 0049), and startup sizing, a fixed roster, a measured request budget and seven slots (ADR 0083)

## Context and Problem Statement

The Mover composition (ADRs 0088 and 0091) changes decisions that ADRs 0044, 0049 and 0083 record, and
those three are `accepted`. An accepted record is not rewritten to change its decision
(`.claude/rules/adr.md`), so their text stays as it is on `main` (`9a1eef5`) and this record carries the
changes, moved word for word from the composition branch (`fire-m-c@6e54014`), plus the 2026-11-12
review changes (findings 3, 4, 5, 11 and 14), each dated where it stands.

## Decision Drivers

* An accepted record keeps the decision it was accepted with; a change has one dated place and its decider.
* Each amended record's other decisions stand.

## Considered Options

1. Edit the three accepted records in place.
2. Supersede each of them whole with a new record.
3. Keep them as on `main`, each with a dated pointer, and carry the changes here.

## Decision Outcome

Chosen: **option 3**. The rule's `superseded by` marks a whole decision replaced; here a few rows of each
change and the rest stands, so each record carries a dated pointer instead, as ADR 0083 records doing for
ADR 0068. ADR 0049's title still names 50 submits a minute; on paper nodes the figure below governs. The
text below is the composition branch's, unchanged, except that a reference to ADR 0087's actor now reads
ADR 0107's (ADR 0087's split, 2026-11-12), and the 2026-11-12 review changes (findings 3, 4, 5, 11 and
14) are new.

### ADR 0044

The table row "A window's length and schedule" reads, for the gap: "60 s between windows (300 s until
2026-11-11)".

* 2026-11-11, the gap between windows (`GAP_SECS`) cut from 300 s to 60 s by the lead's ruling, so back-to-
  back windows lose less of a session to waiting. 300 s was for a paper test waiting on the shared stream
  lock (the constant's docstring until #238), a reason that lapsed when each account got its own stream
  lock. A minute lets the last window's stop and account check leave the per-minute request count (the
  window's own budget is any 60 s, ADR 0091; Alpaca's 200 a minute, ADR 0049) before the next start. If a window started a minute after the last draws a refused stream or a
  rate refusal at start, that is the reason the minute lacked, and the gap goes back with it recorded.
* 2026-11-12, the account check's bounds, for the windows' names (ultra review of the composition, finding
  11, 2026-11-12). A poll of the account check sends at most seven trading requests whatever the names
  held: one that sent a cancel sends no close, one that read the calendar sends no quote and no close, and
  a quote that fails ends the poll. Its deadline is `AFTER_STOP_SECS` (60 s) and two polls (5 s) more for
  each window name held past the first, counted up to `PART_SLOTS` (seven), and a run with `--slots` above
  seven is refused by name (the lead's ruling, 2026-11-12) until slots become a setting, from which the
  deadline, `END_MARGIN_SECS` and the longest window then scale. The bounds count seven names and the extended close's quote:
  `LAST_CLOSE_SECS` 455 to 515 s, `AFTER_END_SECS` 725 to 755 s, `END_MARGIN_SECS` 515 to 575 s,
  `LONGEST_WINDOW_SECS` 2955 to 2925 s (about 49 minutes still), so ADR 0050's deadline sum
  (`LONGEST_WINDOW_SECS` + `AFTER_END_SECS`) is unchanged and ADR 0050 is not amended. Computed by
  importing the module at the composition's commit.

### ADR 0049

**2026-11-11, paper nodes only: both rates are now 180 a minute.** The lead decided, under the owner's
decision of 2026-11-11 that paper carries no risk limit, that the engine's throttle may never be what
refuses a sale: it denies an over-limit submit, and at 50 a Mover node of several slots at its real
re-pricing cadence (about 14 submits a slot a minute) met it before its request gate. Both rates take the
request ceiling the window records already hold (`paper_node.REQUEST_CEILING`, 180, ADRs 0088/0091), so
Mover's soft line and then that ceiling bind first. No new number is introduced; a paper test's tighter
budget, `EmaCross`'s among them, is unchanged. The table above and the reasoning under it are the
2026-11-10 values. `tests/test_risk_limits.py` checks 180 and 181, and that seven slots' 98 submits in a
minute are never denied.

### ADR 0083

Step 2 of the requirement:

  2. **Enter early.** Buy the first rising run of the first leg with a limit order, in every session;
     an extended move only on a pullback and reclaim. Paper size follows the startup account allocation
     (Paper sizing below, replacing risk sizing on paper 2026-11-11); the original risk-sizing row remains a
     legacy simulation setting.

The table's size rows replace "Size from risk":

| Need | Upstream part, as shipped | State |
|---|---|---|
| Paper size | Alpaca TradeAccount.buying_power and cash, divided by configured slots, whole shares through instrument.make_qty | ADR 0091; cash funding limit reproduced against pinned rc8; no margin model |
| Legacy size from risk | `FixedRiskSizer.calculate(entry, stop_loss, equity, risk, …, hard_limit)` (`nt:python/nautilus_trader/risk/__init__.pyi:16-29`, `crates/risk/src/sizing.rs:87-89`) | Explicit simulations only |

Gaps 1 and 2:

1. **The ranking score.** ADR 0087 records the score and its pure inputs; ADR 0107 the actor that
   publishes `movers-rank`; ADR 0091 composes it with a fixed startup roster. Paper delivery is UNVERIFIED until
   the baseline artifact and adapter's external data are present.
   **Composition update, 2026-11-11 (ADR 0091):** the startup sample supplies several qualified names,
   loaded into both providers and the reconciliation/risk maps before the node starts. One Mover
   rotates within that fixed roster; each name has its own entry share cap and position monitor.
   This enables several slots without Controller; runtime admission of a new name remains unbuilt.

After the starting rows:

The lead's later 2026-11-11 paper instruction supersedes that size row: read reported buying_power at
each startup and divide by actual configured slots. The adapter models CASH, so after reproducing its
free-balance denial the instructed conditional engine limit makes the figure min(buying_power, cash)/slots,
named in the record. This uses no numeric monetary policy constant, extra multiplier, DTBP or PDT field.
Paper Mover does not use FixedRiskSizer or risk to size, and the `flow_share` clamp still applies; the older rule remains explicit
in simulations. ADR 0091 carries the shared figure through selection, entries, caps and the rank actor.

Step M2:

* **M2.** A fixed startup roster, re-entry and rotation in one Mover (ADRs 0088/0091). The current
  paper rows are seven held slots in every part (ten and one before 2026-11-12), configurable by `--slots`,
  with `--spares` additional names (default 2, UNCALIBRATED). Entries have no count bound; a sale
  frees its slot for a reclaim after the cooldown or another ranked name until wind-down. ADR 0107's
  actor re-ranks the loaded roster; missing/stale/invalid ranking means no entry. A paper run of this
  composition is UNVERIFIED; Controller is reserved for future runtime admission outside the roster.

Paper sizing, built:

This replaces the window's former sizing
(`mover.py`) on paper; ADR 0091 carries the same text for the window. Built with the composition
(2026-11-12): each Mover node's entry is `min(buying_power, cash) / slots` from one startup account read
(`session_window`), taken in whole shares by `Mover` with `size_from_budget=True`, and the engine's per-order
cap is that entry times `OUTER_BOUND_MULTIPLE`. `FixedRiskSizer` at `risk` 0.01 remains only for explicit
simulations.
A budget-sized entry is min(budget / price, `flow_share` of the last `flow_bars` bars' dollars / price,
the name's start-time share cap) (`mover.py`; `flow_share` 0.05 by default, kept on paper), recorded as `cap_bound` when the
cap binds; neither the share cap nor the name's position monitor is rewritten after the start, so a price
that fell since the start sizes at the cap and does not trip the monitor, and a trip means a real fault
(the lead's change; ultra review of the composition, finding 14, 2026-11-12).
On a reported account each fill asks for the account (`Strategy.query_account`, the adapter's
`_query_account`, one `GET /account`) before the next buy is sized; a buy filled since the last report is
subtracted from free cash, a sale's proceeds count only after a report has them, and every working entry
counts at its open quantity. The live query's routing is UNVERIFIED until a paper run (ultra review of the
composition, finding 3, 2026-11-12).


**The request budget, measured 2026-11-11 (Task M gap 7).** The earlier 19/47-per-order planning
coefficients were UNVERIFIED; ADR 0076 is absent, and they are replaced by the following offline
measurement, not promoted to a worst-case guarantee. Alpaca's own archived Trading API documentation
(Evidence, "Rate Limit") states 200 requests a minute per key, including paper. That is the default
`request_limit_per_minute` setting, never a value obtained from an account. ADR 0088's 180-in-60-s
failure ceiling and Mover's existing entry soft line stay in force. Current paper/live enforcement
and per-key versus per-account scope are UNVERIFIED, as ADRs 0049 and 0069 record.

`mise exec -- uv run --locked pytest tests/test_request_budget.py` runs a scripted workload on the
pinned NautilusTrader `BacktestEngine`, cash/netting with quotes and the native 50 submits/minute
setting. In each slot a limit buy fills, a limit sell fills, a buy-back fills, and a fourth limit
ends flat, all within one minute. With chasing enabled, the first sale rests, is canceled after five
seconds and resubmitted on the following quote, eight times before filling; every replacement's
price comes down. The generated submit/cancel commands are then replayed through the actual
`AlpacaExecutionClient` and alpaca-py to an in-memory `HTTPAdapter.send` boundary. The backtest checks
fills and final positions; the second stage counts HTTP attempts, including the SDK's retries, with
regular and extended-hours flags. A fresh instance of the normal request counter on the simulation
clock independently confirms totals and both peaks without adding synthetic traffic to other tests.
It does not replay Mover's ranking, cooldown or entry decisions.

For this fixture's HTTP 200 submits and HTTP 204 cancels with a known venue order ID, a submit costs
one POST and a cancel/resubmit pair one DELETE plus one POST, in either session.
Entry/sale/re-entry therefore costs three requests per slot; the flat ending adds one. Six open-order
checks and one position check add seven GETs. For N slots and C chase pairs per slot this fixture
measures **7 + N × (4 + 2C)** requests in its minute, including the flat ending. Injecting three 504
responses before success on every check measures **28 + N × (4 + 2C)**: each of the seven GETs really
makes four transport attempts. Order commands are sent once. The fixture counts its own minute
buckets; it also verifies that every fill and attempt is in that minute, so its rolling peak agrees.

| Measured workload (either order flag) | 1 slot | 2 slots | 3 slots | 4 slots |
|---|---:|---:|---:|---:|
| Cycle and flat ending, no chase | 11 | 15 | 19 | 23 |
| Same, three retries per check | 32 | 36 | 40 | 44 |
| Eight chase pairs per slot | 27 | 47 | 67 | 87 |
| Same, three retries per check | 48 | 68 | 88 | 108 |

Four slots use 48 submits in the chase fixture, below the unchanged native 50; the measured peak of
108 is below both 180 and the 200 setting, so this measurement requires no slot reduction. If a
broader workload exceeds its configured budget, reduce the slot setting and repeat it; never raise
the checks to admit it. These rows exclude startup, post-stop account checks, missing-fill catch-up,
reconciliation beyond the periodic open/position checks, replace-chain reads, partial fills, submit
403/422 refusal lookups (`_refused`), cancel 422/fill-race lookups (`_cancel_refused`), unknown-venue-ID
cancel lookups (`_cancel`), all retries of those GETs, and other processes using the same account.
Their aggregate maximum and a reservation for exit headroom before
entry remain UNVERIFIED; the sampled soft line and post-run failure check are not such a reservation.

Every window record now names its budget setting, average requests/minute over the process observation
(module import before preflight through cleanup), UTC-minute peak and its timestamp, rolling 60-s peak,
and remaining rolling headroom, per sanitized account number and API. Market-data requests are retained
without applying the Trading API limit to them. Running and refused records name the setting too.
The provider keeps peaks but **no minute buckets**: the record says `minute_buckets_kept: false`, and
the average is not a minute-by-minute reconstruction. Adding sanitized buckets later belongs to the
Claude lane; this change neither wraps nor edits the credential-handling provider.

**Interrupted measurements (budget reader P2-2, 2026-11-11).** A record write normally refreshes both
request representations from the sanitized counter. Shutdown captures one final snapshot after
cleanup and log preparation, checks that snapshot and persists it without re-sampling. A preparation
error still attempts one final snapshot before writing the failure. `request_budget.measurement_status`
distinguishes running, complete, interrupted, failed,
refused and unfinalized snapshots. `calibration_eligible` is true only for an explicitly completed
measurement with a known positive observation interval; the window additionally needs an `ok` result
and an end timestamp. Ctrl-C, SystemExit and an abandoned/timed-out node mark it interrupted even
after cleanup takes a final snapshot. A process killed without running Python finalizers cannot
produce a final count; its last running snapshot already carries `calibration_eligible: false` and
must not be treated as a full-window observation. No minute history or missing final traffic is
reconstructed from it.
An unavailable counter leaves prior counts explicitly marked with `snapshot_error` and excluded
from calibration, while the primary window failure is preserved. A bad observation clock retains
the counted attempts with an unknown average, `observation_error`, and no calibration eligibility.

**Seven slots in every part, and the backstop (the lead's decisions of 2026-11-11).** `Mover`'s own chase
cadence is `chase_secs` 5 plus the one-second step: about ten cancel/resubmit pairs a minute for a slot
whose sale rests the whole minute, so C = 10 and a slot costs 4 + 2 × 10 = 24 requests in its worst
minute. Seven slots give 7 + 7 × 24 = **175** in the measured fixture's terms, under the 180 failure
ceiling and the 200 setting; with three retries per check, 28 + 168 = 196, still under 200 but over 180,
so a minute of retries at full chase is the failure the record names, not a budget. The lead set seven
slots in every part, overnight and pre-market included (L3 carries the numbers); a raise comes only
from two firings' measured peaks. NautilusTrader's own throttle is raised to the same 180 a minute for
submits and modifies on paper nodes (ADR 0049's dated line), since at 50 its failure handler denies an
over-limit submit, an exit included (`crates/common/src/throttler.rs`, `crates/risk/src/engine/mod.rs` at
the pin). No separate Mover submit gate is added. The backstop: at `soft_line` (140 in the last 60 s)
`Mover` sends no new entry and no chase; exits and cancels still go. **Limit:** failing a window only
when an exit is refused for rate is not built; `request_failure` (a rolling peak of 180 or any 429)
stays the failure for the first firings.

The sub-penny case: Answered later on 2026-11-11: the adapter now sends such a limit on
  Alpaca's cent, a sell rounded down (ADR 0030), and the regression above passes through it.

### From the composition's ultra review, 2026-11-12

* The window reads Alpaca's calendar two weeks back, so the registered arm's five earlier sessions are
  held in the week after a market holiday; a live entry context holding fewer earlier sessions than the
  arm takes is marked missing by name and admits no entry (ultra review of the composition, finding 4,
  2026-11-12).
* A failed native history response leaves the name's coverage where it was and is asked again in a later
  minute, at most once a minute (ultra review of the composition, finding 5, 2026-11-12).

### Consequences

* Bad: seven slots in every part (2026-11-11) rest on single-name windows, the measured fixture and
  `Mover`'s chase cadence, not on a firing's measured peak; pre-market and overnight have no measured figure
  of their own, and two firings' peaks come before any raise.
* Bad: the measured four-slot workload is bounded, but does not establish a maximum for missing-fill
  catch-up, startup, shutdown or all traffic on the account; admission with reserved exit headroom is
  still unbuilt.
* Bad: a reader of ADRs 0044, 0049 or 0083 must follow the pointer here for the paper values.
* Bad: this record runs past the ~130 lines after which `.claude/rules/adr.md` splits one. It is kept whole
  because it moves one branch's changes to three records together; a split into one record per amended
  ADR is the remedy if it grows.

### Confirmation

* Composition update, 2026-11-11: ADR 0091 replaces the single-name/one-entry window with a fixed
  qualified roster and actor generations, per-name share/position bounds and all-symbol account
  checking. The registered listing arm is the default in its 04:00-16:00 scope with explicit Day
  history; outside it the window records its bridge entry arm and reason. Simulations exercise
  several held names, re-entry, rotation and failed/missing input refusal. Paper delivery of the
  composition is UNVERIFIED; no performance or calibration claim follows from these checks.
* `tests/test_risk_limits.py` checks 180 and 181, and that seven slots' 98 submits in a minute are never
  denied (ADR 0049's line). No test checks the 60 s gap itself.

## Evidence

Read 2026-11-12, except where a line says otherwise; the three lines carried from ADR 0083 (Alpaca's
archived documentation, `nautilus-trader` and `alpaca-py`) were read 2026-11-11. Resolved means read in a local copy at the commit
`canon/upstreams.lock.json` records.

* ADRs 0044, 0049 and 0083 at `main` `9a1eef5`, and the composition's edits to them at `fire-m-c@6e54014` —
  the text moved here. Read with `git diff`.
* The 2026-11-12 statements (findings 3, 4, 5, 11 and 14) — `fire-m-c` commits `6e54014..60d4ad2` and
  `bdd0e95`; their shas on `main` follow the landing. Read.
* https://raw.githubusercontent.com/alpacahq/alpaca-docs/master/content/api-references/trading-api/_index.md
  — Alpaca's archived documentation, "Paper Trading" and "Rate Limit": 200 requests every minute per
  API key, 429 on excess. Fetched for Task M gap 7; current paper/live enforcement and per-account
  versus per-key scope are UNVERIFIED, not measured here (ADRs 0049/0069).
* `nautilus-trader@d53849b216e2`: `python/nautilus_trader/backtest/__init__.pyi` (`BacktestEngine`,
  `add_venue`, `add_data`), `trading/__init__.pyi` (`Strategy`), `live/clients.py` (execution hooks),
  `live/providers.py` (`InstrumentProvider.add`) and `common/__init__.pyi` (`Clock.new_test`). Resolved
  for the fast-cycle simulation; no upstream code was run from the read-only source copy.
* `alpaca-py@c42add7c0a70`: `alpaca/trading/client.py` (`submit_order`, `cancel_order_by_id`,
  `get_orders`, `get_all_positions`), `alpaca/common/rest.py` (`_request`) and `common/constants.py`
  (three retries). Resolved; the installed pinned packages run in `tests/test_request_budget.py`.
