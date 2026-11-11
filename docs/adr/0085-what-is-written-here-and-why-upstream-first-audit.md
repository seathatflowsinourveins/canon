---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-11 (ADR 0074); owners of the replaceable rows set by the lead
review-by: 2026-12-11
---

# What is written here and why: the upstream-first audit of 2026-11-11, about 23 lines replaceable now, about 1,086 kept for a chosen behaviour, the rest ours

## Context and Problem Statement

The owner decided on 2026-11-11 that the upstream repositories are the source of truth, and that what they
ship is called, configured or integrated as shipped rather than written here. Independent reader audits
of the adapter, the gate and the runners compared the trading code with the pinned upstreams, and a merge
reproduced their top rows at `main` 4efc962. This is the page a session reads before writing anything:
what can go now, what stays because a behaviour was chosen, and what no upstream was found to cover.

## Decision Drivers

* The owner's instruction of 2026-11-11 above: a function that re-implements an adopted upstream is a finding.
* Every count is a measurement at a named commit.
* The trading path still needs a failing test first and a simulation or paper run (ADR 0074).

## Considered Options

1. Record the measured inventory, with an owner for each replaceable row and a reason for each kept one.
2. Delete everything a reader called replaceable, conditional rows included.
3. Record nothing, and let each change re-audit before it writes.

## Decision Outcome

Chosen: **option 1**. Option 2 drops behaviours that no upstream offers; option 3 repeats a 13,099-line read.

**The count at 4efc962**: 13,099 lines. The adapter (`packages/canon-alpaca/src/canon_alpaca/`, 6 files)
is 5,893; runners and strategies (`session_window`, `session_ma_cross`, `ma_cross`, `paper_node`,
`sandbox_node`, `position_cap`, `backtest`) 3,662; the gate (`ledger`, `ranking`, `returns_matrix`,
`variant_study`) 3,164; `bars_to_catalog.py` and `canon_trading/__init__.py` 380. At 26eb8ef the gate is 3,351.

**Replaceable now, about 23 lines.** The merged list counted about 37; the verifier found that row 2
removes none at rc8, so it is listed as a policy change, not a deletion. Rows 1 and 2 are on the trading path.

| # | Ours | Upstream at the pin | Owner |
|---|---|---|---|
| 1 | `SessionMaCross.on_historical_bars` feeds the warm-up bars to the averages (3 lines); the docstring says the opposite | The response path already feeds them (`handle_indicators_for_bars` runs before `on_historical_bars`), keyed by `id_spec_key`, which ignores the aggregation source. A **defect**: with fewer history bars than `slow_period` the slow average initializes on duplicates | The fix under way on 2026-11-11 |
| 2 | Submission recovery left at `ResolveLocally` (0 lines) | `submission_recovery_policy=RETAIN_UNRESOLVED`. An ACCEPTED order still gets `NOT_FOUND_AT_VENUE`, so `LOCAL_REJECTIONS` and its alias stay; the node's stop then returns an error naming unresolved submissions | The in-flight submission work |
| 3 | `data.bar_interval_ns` (3) | `BarSpecification.get_interval_ns` | The next adapter change, first and smallest |
| 4 | `execution.fill_time` (3) | `dt_to_unix_nanos(str)`; gains sub-microsecond digits if Alpaca sends them | Same |
| 5 | `release_after` in both `data.py` and `execution.py` (~11) | None: an internal duplicate, merged into one | Same |
| 6 | `paper_node.ema_cross_config`'s `strategy_id` and `order_id_tag` (2) | `EmaCrossConfig` defaults `EMA_CROSS-001`, `001` | The 24/5 paper node work |
| 7 | `paper_node`'s `.with_delay_shutdown_secs` (1; the constant stays, other modules use it) | `timeout_shutdown` default 5 s | Same |

**Refuted as drop-ins.** `execution.wire_price` stays: `Price.as_double()` differs from
`float(Decimal(str(price)))` by one ULP from **$14,757.41** upward at 2 decimals (`59030.20` gives
`59030.200000000004`), and alpaca-py sends the float as JSON. `whole_quantity` (a REST decimal string) and
`whole_shares` (a market-data float through `instrument.make_qty`) are not duplicates; merging changes the
refusal inputs.

**At the rc9 pin move, two event-type fixes, no lines.** rc9 adds `generate_order_modify_denied` and
`generate_order_cancel_denied` (absent at rc8). The local modify refusal and the cancel never sent after a
connect timeout (ADR 0039) then emit `...Denied` instead of `...Rejected`; consumers of rejections follow.

**Conditional, about 1,086 lines gross** (ceilings overlap). Each goes only with the behaviour named:

| Ours (approx. lines) | Behaviour lost with the upstream call | Chosen by |
|---|---|---|
| `_AccountCheck` 220, `_flatten_after_stop` 69, unsettled set 57 | Post-stop, symbol-bounded venue-flat check and close | ADR 0060 |
| Subscribe resend ~80 | alpaca-py sends the subscribe frame before `_running = True`; a subscription added between is lost | alpaca-py's race (Evidence) |
| Single-send of order commands ~55 | alpaca-py repeats POST/PATCH/DELETE on 429/504 | ADR 0039 |
| `SessionMaCross` config and strategy ~160, limit timeout 54 | Extended-hours marked DAY limits, long/flat signal; GTD is not in Alpaca's times in force | ADR 0044, ADR 0038 |
| `MaCross` 60 | A different strategy from `EmaCross`; the study's identity | ADR 0048, ADR 0053 |
| `logged_failure` ~35 | The exception text on the disconnect path | none; UNVERIFIED |
| `load_ids_async` 35 | Upstream fetches every asset and drops unknown IDs | none (the adapter's) |
| Exact price and quantity ~35 | Sub-tick and fractional values refused, not rounded | ADR 0036, ADR 0030 |
| `wire_price` 3 | Wire precision above ~$14.7k | none (measured, above) |
| `PositionCap` 35 | Account-wide trip and shutdown, peak and trip metrics | ADR 0044 |
| `run_backtest_config` and payload ~57 | Bar and instrument provenance the ledger keeps | ADR 0041, ADR 0053 |
| `drain` ~29, `_order` seam 4 | Idle grace and abort bound; a test seam | none (the adapter's, the tests') |
| `_cancel_all_orders` ~22 | Cancel scoped to the strategy's orders | ADR 0036 |
| Report `_fill` 9, counters 5 | Report schema; unbounded counts | ADR 0060, ADR 0044 |
| `ledger._ledger_lock` ~25 | The SQLite storage; Optuna's journal is another format | ADR 0035 |

**The rest, about 11,600 lines, is ours: gap or glue.** It is the remainder after the tiers above, not a
count of proven gaps; the readers' searches are listed in Evidence, and an absence beyond them is UNVERIFIED.

* **Adapter, ~5,590**, of which ~961 are contract headers: the replace-chain state machine (Alpaca's PATCH
  mints new IDs), reconciliation reports, submit, cancel and modify with 422 and ambiguity recovery, the
  stream thread and lock layer, the HTTP timeout, data-only guard and request counter.
* **Gate, ~3,140**: queue and retirement protocol (Optuna's `ask` pops the oldest WAITING trial, no
  compare-and-set), ranking refusals, config identity. The DSR is a direct skfolio call; skfolio has no
  effective-N estimator, and NautilusTrader's Sharpe is annualised by √252, which ADR 0031 does not use.
* **Runners, ~2,870**: session tables, the window report, session and account policies; 906 classed
  gap and 649 glue by their reader.

### Consequences

* Good: a session sees, before it writes, which rows to take and which lines not to touch.
* Bad: line numbers drift as main moves; rows name functions so they can be found again.

### Confirmation

* Review treats a new function that re-implements an adopted upstream as a finding, and reads the
  conditional table before a deletion. Each replaceable row closes with its owner's change.
* **2026-11-11, `bars_to_catalog.py`**: one reader pass at 26eb8ef (356 lines, 18 rows). Ingestion already
  goes through upstream's catalog write-back; nothing is replaceable now. The 25-line result facade
  (`CatalogBars`, `catalog_bars`) goes only by changing the return API and its callers. Its docstring
  (lines 57-59) omits the adapter's HTTP timeout. Runtime equivalence UNVERIFIED.
* **UNVERIFIED:** `canon_trading/__init__.py` (24 lines); the overlap of the 232 adapter lines added since
  the audited commit; Alpaca's `transaction_time` nanoseconds; `drain` timing; the backtest and
  position-cap ceilings; the gate rows beyond queue, N-hat and session days; the runtime reproduction of row 1.

## Evidence

Read 2026-11-11 at the pins through `scripts/canon src`. A second reader (`evidence-verifier`, 2026-11-11)
corrected the replaceable total, row 2, six "Chosen by" entries and the gate's spot-checked rows.

* Line counts — `git show 4efc962:<file> | wc -l` over 19 files, and at 26eb8ef — measured here (git, this repository).
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e2/crates/common/src/python/response.rs (357-363) and crates/model/src/data/bar.rs (910-921) — indicators fed before the callback; key ignores aggregation source (row 1) — resolved.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e2/crates/live/src/execution/submission.rs (45-51), manager.rs (3587-3605) and crates/live/src/node/config.rs (797) — the policy; ACCEPTED orders still get `NOT_FOUND_AT_VENUE`; the stop returns an error (row 2) — resolved.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e2/crates/model/src/python/data/bar.rs (162-165) and python/nautilus_trader/core/datetime.py (81) — `get_interval_ns`, `dt_to_unix_nanos` (rows 3, 4) — resolved.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e2/crates/trading/src/examples/strategies/ema_cross/config.rs (37-41) and crates/live/src/node/config.rs (800-802) — `EmaCross` identity defaults; 5 s shutdown default (rows 6, 7) — resolved.
* https://github.com/nautechsystems/nautilus_trader/blob/da6b57aefdd6/python/nautilus_trader/live/clients.py (493, 531) — the two `..._denied` methods; absent in rc8 — fetched (rc9 candidate, not the pin).
* https://github.com/alpacahq/alpaca-py/blob/c42add7c0a70/alpaca/data/live/websocket.py (519-524) — subscribe sent before `_running = True` — resolved.
* `wire_price` — every 2-decimal price 10,000.00–99,999.99: first difference at 14,757.41 — measured here in the locked venv (nautilus-trader 2.0.0rc8).
* https://github.com/alpacahq/alpaca-py/blob/c42add7c0a70/alpaca/common/rest.py (127-136, 196-206) and alpaca/common/constants.py (11-13) — the 429/504 repeat of every method — resolved.
* Searches for the remainder, as the readers report them: NautilusTrader rc8 and rc9 (`live/clients.py`, `crates/execution`, `crates/live`, `crates/network`, `crates/analysis`, the strategy examples), alpaca-py (`trading`, `data/live`, `data/historical`, `common/rest.py`), the two community Alpaca adapters in the cache, skfolio and Optuna — UNVERIFIED beyond the rows reproduced above.
