---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Paper session windows: what a window knows at start-up, how it warms up, and what starts it

## Context and Problem Statement

ADR 0044 runs `SessionMaCross` on paper in bounded windows, one node per window. NautilusTrader's
reconciliation page advises: "Persist all execution events to the cache database." What can a node with
this repo's Python adapter keep between windows, what does a window therefore know when it starts, and
how does it start from the venue's truth without a phantom position and without a 20-minute warm-up? And
since nothing starts a window by itself, how do windows run through the weekday sessions?

## Decision Drivers

* Upstream's own features and patterns first (ADR 0001); a window starts from Alpaca's state, not ours.
* A window placing nothing until its averages warm up wastes two thirds of a 30-minute window.

## Considered Options

1. Cache database backing (Redis or PostgreSQL; strategy state needs Redis at the pin) to carry state.
2. No backing: each window rebuilds from the venue at start-up, and its averages from the adapter's bars.

## Decision Outcome

Chosen: **option 2**, the only one available. Upstream's adapter guide at the pin: "Redis/PostgreSQL cache
backing is unsupported with custom Python clients in either launch mode. Both modes drive asyncio, where
the backing's blocking worker calls would stall the event loop." Strategy state persistence
(`with_load_state`, `with_save_state`, `on_save` and `on_load`) "requires a Redis backing" at the pin
(configure_live_trading; later releases add Postgres). The guide's own exit, "Native-only nodes can use
database backing with `run()`", is closed to a node with a Python adapter.

**What a window knows at start-up**, from start-up reconciliation against Alpaca alone: every open order
however old; an explicit position report for the symbol, flat included, since the adapter reports each
loaded instrument with no position as flat ("An explicit position report is authoritative", reconciliation
page, "Bounded history safety"); and the order and fill history inside `reconciliation_lookback_mins`.

**The lookback stays at one minute**: every window and the `EmaCross` paper test start only after finding
the account flat, and the flat report is authoritative, so older history adds Alpaca calls and nothing
needed. Upstream's readings differ: the docs' tip leaves it unset (here the newest 500 orders and up to
1,000 fills), the Lighter tutorial uses 60 minutes, the adapter's paper tests one or two. Each should be
safe from ADR 0037's phantom with the flat report (the docs, the adapter's offline test); UNVERIFIED on paper.

**Lost between windows:** the averages, the cap's peak, the order and position history. What a window
leaves is not the next one's: the next refuses to start unless the account is flat.

**The warm-up.** `SessionMaCross(warm_up=True)` follows upstream's EMA cross `on_start` (concepts/strategies,
"Handler example": "Registered indicators receive the bars from a request response before
`on_historical_bars()` runs, so a request hydrates them"): before the live subscription it requests at
least an hour of the adapter's one-minute `EXTERNAL` bars from IEX; the live bars stay the `INTERNAL` ones
built from IEX trades. One departure: the averages are registered for the live type only, and
`on_historical_bars` feeds the history to them, oldest first, only while no live bar has reached them, since
a registered history type would also take an answer arriving after a live bar or the 60 s timeout; such an
answer is dropped and counted. Decisions wait for completion or the timeout; the report gives the counts.

**What would close the gap upstream** (this repo's reading; no issue or plan found): database backing for
custom Python clients; with it and Redis, `save_state` and "persist all execution events" would apply.

**The schedule** (no upstream schedules bounded windows; each part below follows the upstream named):

| Part | How | Follows |
|---|---|---|
| What fires | A second launchd user agent, `local.canon.paper-session`, installed, reported and removed by `scripts/automation` beside the lane watchdog; every 2100 s, not at load; a firing while the job still runs or the Mac sleeps is missed (launchd.plist(5)) | ADR 0011's launchd agent |
| What runs | `~/.claude/canon/paper-session.sh` brings its own clone, `~/.claude/canon/paper-checkout`, to the `main` the origin has at that firing, detached, or fails the firing, then runs `mise run paper:session --session auto --minutes 25` there: one window, one node, one process, disposed, its records naming the commit. 25 minutes, with about two of start-up and stop, leaves the 300 s gap inside 35 | configure_live_trading: "run nodes as standalone Python scripts or services"; "One LiveNode per process"; actions/checkout's reuse of a directory (the runner's header) |
| Which session | `auto` takes the regular session when Alpaca's clock says it is open (`is_open`, which is false on weekends and holidays), the extended one when `EXTENDED_READY` and it is open, and otherwise prints "nothing to run" and exits 0 | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| A window that dies | It writes `latest.json` as `running` before the node is built and `ok` or `failed: <why>` after, the account check included; a refusal someone must look at (account not flat) is `refused: <why>`, while a skip that clears by itself (the gap, under 360 s of session left) exits 0 and keeps the last result. A `running` record past its `stale_after` (70 minutes) is a window that died | No upstream |
| Status | `scripts/automation status`'s `paper session` row: the last window, its result, age and leftovers; FAILED when the last firing exited non-zero (one that ended before Python wrote anything); STALE when no run, a window or not, wrote `last-firing.json` for two intervals | ADR 0011's status rows |

Installing it is the owner's step: `scripts/automation install`. Whether the agent's `mise run` reads the
login keychain without a dialog is UNVERIFIED until its first firing. A firing while the Mac sleeps is missed.

### Consequences

* Good: a window decides from its first live bar; every start is from Alpaca's state.
* Bad: history bars (Alpaca's aggregation) and live bars (the engine's, from trades) are built by two
  different aggregators from the same feed; a minute with no IEX trade has no live bar.
* Bad: an answer later than the first live bar is dropped, and that window warms up on live bars.
* Bad: one paper account still serves the windows and the paper tests; a test can lose the lock to a window
  (ADR 0044). A window refused for an account that is not flat stays refused until someone clears it. A
  node that hangs on stop (ADR 0044's disconnect hang) is waited for at most `STOP_TIMEOUT_SECS` (about
  115 s); the window is then recorded failed, the account checked (at most `AFTER_STOP_SECS`, 60 s, and a
  30 s settle, each Alpaca call within the adapter's timeouts), and the process ended (ADR 0044).

### Confirmation

Offline, in `scripts/ci` (`tests/test_session_window.py`): on NautilusTrader's `BacktestEngine`, with 30
`EXTERNAL` one-minute bars in a `ParquetDataCatalog` and live `INTERNAL` bars built from trades, the
history is fed and the averages are initialized at the first live bar; without the warm-up they are not.
An answer after a live bar or after the timeout is dropped, not fed; one in any order is fed oldest first.
The request's callback ran on failure too (no catalog), in a probe outside `scripts/ci`, as actors.md
documents.
On paper: UNVERIFIED until a regular window runs.

The schedule, offline: `--session auto` picks the session from a stand-in clock and calendar and runs
nothing on a Saturday; a refusal, a failure after start and a failed account check become the latest
record, after `running`; a skip exits 0 and keeps the last result; every run writes its heartbeat
(`tests/test_session_window.py`). After a second reader's review (2026-11-10), each first shown by a
failing test there: a window that never ran (its node not built) no longer writes the gap marker, so a
retry is not skipped as if one had ended; a refused window now updates the heartbeat. `scripts/test-lanes`
rehearses the install with `AUTOMATION_SKIP_LAUNCHD=1`
and a stub `launchctl`: the agent's arguments, interval, working folder, `RunAtLoad` and the interval
arithmetic; the row for ok, running, died, a record's own `stale_after`, failed, refused, no record, a
non-zero exit under an ok record, and three silent hours; the uninstall. Not installed on this machine.
The same test fires the runner at a stand-in origin: each firing runs its newest `main`, detached, with
`CANON_COMMIT` and a kept `.venv`; a failed fetch runs nothing and fails the row; uninstall removes runner,
clone and its mise trust. On GitHub: a fresh clone 0.5 s, its environment 6.6 s, a kept one 0.4 s. One
firing from GitHub's `main`, run with no environment but `HOME`, read the pair, found no session open and
recorded its commit; it first failed until the runner exported `PATH`, which mise's `exec()` needs (observed).

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md, "Database cache backing";
  the same warning at the pin's `docs/developer_guide/python_adapters.md` (fetched, raw).
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: `with_load_state` and
  `with_save_state` need a Redis or Postgres backing (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md: "Persist all execution
  events to the cache database"; "Bounded history safety"; the lookback tip (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/strategies.md, "Handler example"; the same at the pin
  (fetched, raw).
* nautilus-trader 2.0.0rc8 in `.venv`: `LiveNodeBuilder.with_cache_database_factory` exists, which the
  guide rules out for Python clients; `Strategy.request_bars(..., callback=)` (`bundled`).
* `packages/canon-alpaca/src/canon_alpaca/execution.py` on `main`: a flat report for each loaded instrument
  with no position; its offline test `test_older_filled_orders_and_no_position_at_alpaca_end_start_up_flat`
  (read). The adapter's paper tests' lookbacks: `test_paper_matrix.py` 1, `test_paper_fill_reports.py` 2.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/actors.md,
  "Python callbacks and joins": a request callback "runs once on terminal success or failure" (fetched by
  the second reader, raw).
* The Lighter tutorial at the pin, `docs/tutorials/lighter_rwa_composite_mm.md`: 60 minutes (fetched, raw).
* Second reader (`evidence-verifier`), 2026-11-10: corrected the state backing (Redis at the pin), the
  default history, the lookbacks, who starts flat, the callback's source and the upstream-gap wording.
* ADR 0044: the windows, the lookback's first value and the phantom it removed (read).
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: "run nodes as standalone
  Python scripts or services"; "One LiveNode per process" (fetched, raw).
* `scripts/automation` and ADR 0011: the launchd agent, its install, status and uninstall (read).
* `man launchd.plist` on this machine: `StartInterval` "causes the job to be started every N seconds",
  a firing while the Mac sleeps "will be missed", and "If the job is running during an interval firing,
  that interval firing will likewise be missed"; `RunAtLoad` defaults to false; `WorkingDirectory` (read).
* Second reader (`evidence-verifier`) and `/code-review` (high), 2026-11-10: the 35-minute interval left no
  room for the gap, a failed account check was recorded `ok`, status missed a failing or silent schedule,
  routine skips looked like refusals, the agent pointed at a worktree; all fixed above.
* The offline tests above (run).
* Correction, 2026-11-10: the agent ran in the main checkout, which nothing kept on `main`; a clone fetched
  at each firing replaces it, same intent. actions/checkout v7.0.1 at `3d3c42e5aac5`, `src/git-*.ts`; mise
  v2026.11.5 `docs/cli/trust.md` (`mise run` trusts its config; observed, `--untrust` undoes it); uv 0.13.7
  `docs/concepts/projects/sync.md` (`uv run` is inexact without `--exact`) (all fetched, raw); `man git-clean`
  (`-x` keeps `-e`) and `man launchd.plist` (`AbandonProcessGroup`) (read).
* A second reader's review of `main` (2026-11-10): the gap marker after a window that never ran and the
  heartbeat after a refusal, both reproduced offline and fixed (run).
