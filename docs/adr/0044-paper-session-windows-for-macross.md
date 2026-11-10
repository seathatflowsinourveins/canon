---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Paper session windows: `MaCross` guarded for live use, native runtime checks, a one-share cap in the node

## Context and Problem Statement

The owner wants the end-to-end paper run to go around the clock on weekdays. ADR 0037 showed upstream's
`EmaCross` builds a one-sided position on Alpaca and is fit only for a bounded check. Which strategy can
run a whole session unattended, long or flat at one share, with limits outside the regular session, and
how does a long run share the one paper account with the paper tests?

## Decision Drivers

* The engine's own features before code of ours (ADR 0001); a position never over one share (ADR 0009).
* A run evaluates nothing: no ranking, no recommendation (ADR 0010).
* Recorded backtests keep their meaning: `MaCross` itself is not changed.

## Considered Options

Strategy: 1. `EmaCross` with a native setting that makes it safe; 2. the repo's `MaCross`, extended in a new
module. Sharing: A. a long run holding the paper lock; B. bounded windows under the same lock; C. a second
paper account.

## Decision Outcome

Chosen: **option 2 and option B**. No native setting makes `EmaCross` safe: the risk engine bounds each
order and the submit rate, not a position, and the trading state that would stop increases is not exposed
to Python at the pin. Option A starves the tests; C is the owner's to create (Consequences).

**`SessionMaCross`** (`session_ma_cross.py`) subclasses `MaCross` and fixes two defects it has for live
use: a bar arriving while an order works sends a second (now nothing is sent while one works), and its exit
is a reduce-only market order (observed at the pin), which the adapter denies (ADR 0036). Neither is fixed
in `MaCross` now, since recorded backtests ran its code; before any gated variant runs on paper (ADR 0010's
last step), the backtested and the live strategy must be one code path. Outside the regular session every
order is a DAY limit $0.05 (five $0.01 ticks, since extended hours carry "less liquidity") through the
touch, sent with `params={"extended_hours": True}`; one not filled in 30 s is canceled and its signal
dropped, never chased. The adapter sends that flag (ADR 0038); `WindowConfig` still refuses an extended
window (`EXTENDED_READY`) until one has passed on paper and the disconnect-hang fix is on `main`.

**Each part, and the upstream it follows:**

| Part | Setting or code | Upstream it follows |
|---|---|---|
| Order size and rate | ADR 0037's `risk_limits`: $1,000 per order, 50 submits and 25 modifies a minute | `LiveRiskEngineConfig` (ADR 0037) |
| Orders and position in step with Alpaca | `open_check_interval_secs=10`, `position_check_interval_secs=60`, both off by default; the position check aligns to the adapter's position reports until `position_check_retries` (3) run out | configure_live_trading, "Continuous reconciliation": "5-10s", "30-60s" |
| Start-up reconciliation | Scoped to the symbol and one minute of history: every run starts only once it has found the account flat, so history can only mislead, and 60 minutes could hold the last window's exit without its entry; open orders and positions are reported however old | The Lighter tutorial's `reconciliation_instrument_ids`; the minute is the adapter's own paper tests' (the tutorial uses 60; the docs' tip leaves it unset) |
| The node | `LiveNode.builder(...)`, `with_risk_engine_config`, `with_exec_engine_config`, `with_delay_post_stop_secs(5)` | `examples/live/sandbox/exec_tester.py`, the Lighter tutorial |
| Exit at wind-down and stop, in the session | `market_exit()`, and `manage_stop=True` so `stop()` runs it; `market_exit_reduce_only=False`, `market_exit_time_in_force=DAY` | concepts/strategies, "Market exit" |
| Start flat, confirm flat | Refuse to start unless flat in the symbol; after the node stops, read Alpaca and fail naming what is left (in the session, close at market first) | The Lighter tutorial: "Start with a flat target position", "confirm that no orders or positions from the run remain" |
| The session | Alpaca's clock and calendar before anything is placed | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| Bars | One-minute `INTERNAL` bars the data engine builds from trades; the adapter streams none | The data engine's bar aggregation |
| A position over one share | `PositionCap`, a non-trading strategy in the node (a `DataActor` has no portfolio at the pin): the portfolio's net position every second, `shutdown_system` over one share | No upstream: no setting caps a position, and the trading state is not settable from Python |
| A window's length and schedule | `minutes` (default 30, at most 60), clipped to the session's end, timed from Alpaca's clock; wind-down 120 s before; 300 s between windows; session and feed (IEX) from configuration; overnight refused until its data is observed | No upstream |

What a window knows at start-up, what is lost between windows, and the averages' warm-up: ADR 0047.

A window's node log and JSON report go to the per-user temporary folder, outside the repository. Windows
trade SPY only: the $1,000 cap is sized for it. ADR 0037's `ema_cross_paper_node` gets the same
execution-engine settings and the cap, through one builder, `paper_node.alpaca_paper_builder`.

**Sharing the account.** `paper:session` reads the key pair into its own `env` as `paper:test` does, which
supersedes ADR 0029's clause "Only `paper:test` puts the items in an environment": now those two tasks do.
It waits for the same lock (at most 900 s) and holds it for one window. `paper:test` waits up to 3900 s, so
a test queued behind a window outlasts it.

### Consequences

* Good: a whole session is a sequence of bounded, checked windows on the engine's own reconciliation.
* Bad: a paper test can lose the race for the lock to the next window. What replaces the sharing is a second
  paper account for session runs, as ADR 0010's last step already asks for separate accounts; that is the
  owner's to create.
* Bad: the averages warm up on each window's own bars, 20 minutes of 30 at the defaults, so about 8
  trade before the wind-down. ADR 0047 warms them from the adapter's bars instead (offline; on paper
  UNVERIFIED; a late answer or a thin feed still leaves a window cold).
* Bad: a window does not know half days; Alpaca's clock and calendar give its end.

### Confirmation

Offline, in `scripts/ci` (`tests/test_session_window.py`, `BacktestEngine` with a simulated `ALPACA` cash
venue under `risk_limits`; also passing with the host's zone at UTC and Europe/Berlin): in the session it
trades as `MaCross` with market orders whose closes are DAY and not reduce-only, and winds down by
upstream's market exit; outside it every order is a marked DAY limit through the touch, a resting one is
canceled at its timeout with its signal dropped and no second order meanwhile, and the wind-down closes
with a marked limit; a stop while an order works sends no second exit; a short is bought back; the cap
trips at two shares and holds at one over 3,000 checks; the runtime checks and the scoped reconciliation
are set; the window refuses an extended session while `EXTENDED_READY` is off, overnight, the regular
session when closed, a non-trading day, a SIP feed, over 60 minutes, an account not flat and a start
inside the gap.

On paper, 2026-11-10, after the close: a regular window refused, "the regular session is closed". An
extended window started at 16:12 New York, before the adapter's extended-hours change was on `main`,
against the condition for extended runs; the gate above now refuses that. The cap stopped it at its first
check, 0.3 s into start-up (the window ended 10.4 s after it began): start-up reconciliation, then not
scoped, had inferred a `LONG 10 SPY.ALPACA-EXTERNAL` from earlier filled orders while Alpaca held none
(ADR 0037). No order was placed; the account was flat in SPY. No node log was written: file output needs
`fileout_level`, now set. The cap's `shutdown_system` stopped the node ("Received ShutdownSystem command",
in its log). At 16:27, with the scope at 60 minutes, the EmaCross paper test's node held no position but
its strategy's (no IEX quote came after the close, so it placed nothing); the one-minute scope is
UNVERIFIED on paper until a window runs. While a phantom remains, a window stops at start, failing closed. Waiting: a regular window at
the next open, and extended windows once the extended-hours change and the disconnect-hang fix are on
`main`.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md, "Continuous reconciliation":
  the two intervals, off by default, and their recommended ranges (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md, "Position reconciliation",
  "Runtime checks", "Bounded history safety": what each check compares and aligns (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/logging.md: `FileWriterConfig` (fetched, raw).
* nautilus-trader 2.0.0rc8 in `.venv`: `LiveExecutionEngineConfig`, `Strategy.portfolio`, `submit_order`'s
  `params`, `shutdown_system`; no portfolio on `DataActor`, no trading-state setter (`bundled`).
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading", "Sub-penny
  increments": sessions, eligible orders, "less liquidity" (fetched, raw).
* alpaca-py 0.45.0 in `.venv`, `trading/models.py`: `Clock`; `Calendar`, naive, with `session_open` and
  `session_close` (`bundled`).
* `packages/canon-alpaca/src/canon_alpaca/execution.py` on `main` (ADR 0038): `params={"extended_hours":
  True}` sent only on a DAY or GTC limit, other marked orders and unknown keys denied (read).
* https://github.com/nautechsystems/nautilus_trader/releases: v2.0.0rc9, tagged 2026-11-10, lists
  reconciliation fixes and no position cap or trading-state setter (second reader; not adopted here).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/tutorials/lighter_rwa_composite_mm.md:
  the scoped reconciliation, start flat, confirm flat (fetched, raw).
* `examples/live/sandbox/exec_tester.py` at the pin: the builder calls (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/strategies.md, "Market exit", `manage_stop`; the same
  keys at the pin (fetched, raw).
* https://raw.githubusercontent.com/alpacahq/alpaca-skills/0e7c35163ae462730de1f4a0bfafceac82b7d59b/skills/trading-api/paper-trading/SKILL.md:
  check the clock; 200 a minute "is not stated in Alpaca's current documentation"; every response carries
  `X-RateLimit-*` headers (fetched, raw).
* An independent review of ADR 0037's node, 2026-11-10: nine findings. Fixed here, each first shown by a
  failing test: the unset lookback (phantom position), no position cap, the node's wiring untested, the
  paper test blind to other positions and reading the session once. The flat cap stays SPY's; windows
  refuse other symbols.
  The adapter's three (no HTTP timeout, no stream restart, two matrix rows unasserted) are its own.
* ADR 0037: `EmaCross` on Alpaca, the risk limits, the phantom reconciled position (read).
* The offline tests and the two paper starts above (run).
* Code review (`/code-review`, high), 2026-11-10: the naive calendar times, the ignored params, a second exit
  at stop or wind-down, a short counted as held, start-up eating the wind-down, an unwritten report;
  all fixed above. Second reader (`evidence-verifier`), 2026-11-10: corrected the cap's timing, the node
  log, "only a strategy" and the position check's retries; all applied.
