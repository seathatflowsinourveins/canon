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
| Order size and rate | ADR 0049's `risk_limits`: $1,000 per order, 50 submits and 25 modifies a minute | `LiveRiskEngineConfig` (ADR 0049) |
| Orders and position in step with Alpaca | `open_check_interval_secs=10`, `position_check_interval_secs=60`, both off by default; the position check aligns to the adapter's position reports until `position_check_retries` (3) run out | configure_live_trading, "Continuous reconciliation": "5-10s", "30-60s" |
| Start-up reconciliation | Scoped to the symbol and one minute of history: every run starts only once it has found the account flat, so history can only mislead, and 60 minutes could hold the last window's exit without its entry; open orders and positions are reported however old | The Lighter tutorial's `reconciliation_instrument_ids`; the minute is the adapter's own paper tests' (the tutorial uses 60; the docs' tip leaves it unset) |
| The node | `LiveNode.builder(...)`, `with_risk_engine_config`, `with_exec_engine_config`, `with_delay_post_stop_secs(5)`; a regular window 65 s, since the managed stop's market exit (60 attempts a second apart) runs only inside that delay before the node disconnects | `examples/live/sandbox/exec_tester.py`, the Lighter tutorial; `LiveNode::stop` and `process_runner_for` in `crates/live/src/node/mod.rs` at the pin |
| Exit at wind-down and stop, in the session | `market_exit()`, and `manage_stop=True` so `stop()` runs it; `market_exit_reduce_only=False`, `market_exit_time_in_force=DAY` | concepts/strategies, "Market exit" |
| Start flat, confirm flat | Refuse to start unless flat in the symbol; after the node stops, read Alpaca and fail naming what is left (each poll for 60 s: cancel every open order the check did not send, again until it is gone; once none is open and while Alpaca's clock says the session is open, close at market, again while held; cancel a close that would only queue) | The Lighter tutorial: "Start with a flat target position", "confirm that no orders or positions from the run remain" |
| The session | Alpaca's clock and calendar before anything is placed | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| Bars | One-minute `INTERNAL` bars the data engine builds from trades; the adapter streams none | The data engine's bar aggregation |
| A position over one share | `PositionCap`, a non-trading strategy in the node (a `DataActor` has no portfolio at the pin): the portfolio's net position every second, `shutdown_system` over one share | No upstream: no setting caps a position, and the trading state is not settable from Python |
| A window's length and schedule | `minutes` (default 30, at most 60), clipped to the session's end and to `LONGEST_WINDOW_SECS` (Sharing the account), timed from Alpaca's clock; wind-down 120 s before; 300 s between windows; session and feed (IEX) from configuration; overnight refused until its data is observed | No upstream |

What a window knows at start-up, what is lost between windows, and the averages' warm-up: ADR 0047.

A window's node log and JSON report go to the per-user temporary folder, outside the repository. Windows
trade SPY only: the $1,000 cap is sized for it. ADR 0037's `ema_cross_paper_node` gets the same
execution-engine settings and the cap, through one builder, `paper_node.alpaca_paper_builder`.

**Sharing the account.** `paper:session` reads the key pair into its own `env` as `paper:test` does, which
supersedes ADR 0029's clause "Only `paper:test` puts the items in an environment": now those two tasks do.
It waits for the same lock (at most 900 s) and holds it for one window. `paper:test` waits up to 3900 s, so
a test queued behind a window outlasts it: a window is shortened so that it and its bounded end fit that
wait. After the window's end come the stop (`STOP_TIMEOUT_SECS`, about 115 s), the last of the node's
worker calls (`ADAPTER_CALL_SECS`: alpaca-py's request and its three retries at the adapter's timeouts,
three seconds apart, about 129 s) and the account check (60 s in which polls start; the poll begun
before its end runs on, two reads, the clock and a cancel of each of up to two orders; then a last
reading, the clock and a cancel of what it shows; about 360 s), about 605 s in all (`AFTER_END_SECS`); before its
start, 30 s of start-up and the first clock read, with alpaca-py's retries, 129 s; and the window is timed
on the host's clock, which may run 60 s behind Alpaca's (`SKEW_SECS`). So a window runs at most
3900 - 30 - 129 - 60 - 605, `LONGEST_WINDOW_SECS`, 3076 s (about 51 minutes); a `--minutes 60` window runs
that and its record says so (`shortened`). The reads after the clock read are inside the window, whose end
is absolute, and a window whose start-up reads leave too little of it is skipped. Not counted: in each
reading, one request for each order in doubt and one for a lost close's search, which only a lost answer
brings. The timeouts' sums are not wall-clock bounds (the connect timeout applies per address tried, the read timeout
between bytes, ADR 0039). A window also ends `END_MARGIN_SECS` (455 s) before its session: the check's last
market close goes out at most about 394 s after the end (`LAST_CLOSE_SECS`), and a minute covers the skew,
so it is sent while the session is open. The same sum with `paper:session`'s 900 s wait for the lock fits its 105-minute
deadline. A test reads the 3900, the 900 and the 105 minutes from `mise.toml`.

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

A second reader's review (2026-11-10), each fix first shown by a failing test in the same file: the wait for
the node's stop had no bound (now `STOP_TIMEOUT_SECS`, about 115 s, the node's own stop bounds and one request
summed; past it the window is recorded failed, the account still checked, and the process ended); the market
close after the stop was tried once and keyed on the configured session (now retried each poll for at most 60
s once the cancels land, sent only while Alpaca's clock says the session is open, and an order left open after
the close is canceled); `PositionCap`'s header said the strategies then flatten, which upstream's `EmaCross`
does not (header and README corrected; the cap sends no order); a limit dropped at its timeout was sent again
on the next bar (now not until the opposite cross). Code review (`/code-review`, high) of those fixes: the
process exit keyed on the error type, a Ctrl-C during a hung stop waiting on it, a 60 s end margin shorter
than the stop and the check (then 240 s; now 455 s, above), a short's buy-back or an equal bar moving the
dropped signal; then the window's own Alpaca client without the adapter's HTTP timeout, an error after an
abandoned stop exiting unprinted, and disposal keyed on any abandonment; each shown by a failing test and
fixed.

Two more second readers' reviews (2026-11-10), each fix first shown by a failing test in the same file:

* After an abandoned stop the adapter's shielded REST calls ran on in their worker threads, so an order
  sent after the check read flat was outside it: the node's loop now runs them in the window's own
  executor, shut on abandonment, and the check waits for those running (`ADAPTER_CALL_SECS`).
* The check's 60 s did not bound it: calls before and after its deadline were not counted, and alpaca-py's
  retries of a 429 or 504 ran outside the deadline and after its clock read (a close sent after the
  session closed). The window's client now sends each request once, the check repeats each step under one
  deadline that no call starts after, and the end margin and window length are computed from the bound.
* An open order was canceled once; one whose cancel failed or did not land stayed working and could fill
  unattended: every open order the check did not send is now canceled again each poll, its own close left
  to fill while the session is open.
* An error from a read, the clock or a cancel (an `APIError` or the adapter's timeout) ended the check at
  once: each is now recorded and the next poll goes on.
* A bar crossing the other way while a dropped limit's cancel was pending, or with a short to close, did
  not clear the dropped signal: the cross is now read on every bar before the strategy's early returns.
* The test of a close retried after a 403 sent its only close after the cancel had cleared; a test now
  gets the 403 with no order open, twice.

Code review (`/code-review`, high) of those fixes: the result was the reading from before the last poll's
close (a flat account recorded not flat), the check's own close was canceled when its answer was lost, the
start-up reads had lost alpaca-py's retries, and the bounds were stated as wall-clock bounds; each fixed,
the first three covered by a new test.

A third second reader's review (2026-11-10), each fix first shown by a failing test in the same file:

* The node runs events after the trader's stop only for `delay_post_stop`, then disconnects, so at 5 s a
  managed stop's market exit was cut off after five attempts and the strategy never stopped: a regular
  window's delay is now the exit at its longest and 5 s (`MANAGED_DELAY_POST_STOP_SECS`, 65 s; the stop
  bound, 115 s, already summed the two).
* A connect timeout (an `OSError`) left the run's heartbeat unwritten, or at "a regular window": it, and
  any other error, is now heard as `failed`.
* A limit was canceled once at its timeout and no alert followed, so a cancel rejected or unanswered left
  it working for good, the strategy deciding nothing and its stop closing nothing: it is now looked at
  after each further timeout until it is closed, canceled again when a cancel was rejected, its signal
  dropped once. A cancel that went unanswered leaves it pending cancel, which the strategy can neither
  cancel again (refused at the pin) nor query (the adapter answers no order query); the node's open-order
  check and the window's account check, which cancels in any session, are what clear it. Extended
  windows, the only ones that send these limits, stay off (`EXTENDED_READY`).
* A close that filled in part and timed out held the dropped side, so the shares left stayed open for the
  rest of the cross: a limit that filled in part, by its timeout or while its cancel was on its way, now
  drops nothing.

Code review (`/code-review`, high) of those fixes: a cancel sent again for an order pending cancel sends
nothing (above), a part fill racing the cancel, a part-filled limit reported as a dropped signal, a
heartbeat whose own write failure hid the run's error, the delay set by a second builder call, and
`Runner.close` waiting up to 300 s for a worker call after a clean stop (now at most `ADAPTER_CALL_SECS`,
then the runner is left as after an abandoned stop); each fixed, the first two and the last covered by a
new test.

An independent review of the check as landed (2026-11-10), each fix first shown by a failing test in the
same file:

* A node order whose submit answer was lost (the adapter logs an unknown outcome and leaves it submitted)
  could be taken by Alpaca after the check read the account flat, a live order after an `ok` record: each
  such order is now looked up by its client order ID each poll, the account is not flat while one is not
  found, and one never found is named in what is left, so the window is recorded failed.
* After a close attempt every order first seen was taken for the check's own close, so a late order of
  the node's was left working: the close is the order `close_position` returns, or, when that answer is
  lost, the first new market order on the close's side for the held quantity; any other is canceled.

Code review (`/code-review`, high) of those two: a failed lookup skipped the whole poll, an order the
lookup found but the listing did not yet show was taken as flat, a close Alpaca refused (403, 422) was
taken as one whose answer was lost, the orders in doubt were read before the abandoned node's last calls
had ended, a notional order's empty quantity ended the check, and the order's deprecated `order_type`
was read; each fixed, the first three and the fifth covered by a new test. That Alpaca answers 404 for a
client order ID it does not hold, as the adapter's lookup also reads it, is UNVERIFIED against its
documentation.

Two independent reviews of those fixes as landed (2026-11-10), each fix first shown by a failing test in the
same file:

* The orders in doubt were read from the node's cache after its disposal had cleared it (`Cache.reset`,
  at the pin), so on a clean stop none was looked up: they are now read before the disposal, and again
  after the wait when the stop was abandoned.
* The node's own in-flight check rejects a submit never acknowledged, with the reason `INFLIGHT_TIMEOUT`
  (`create_reconciliation_rejected`, at the pin); that rejection was counted as Alpaca's answer, and is
  now not.
* A close whose answer was lost was left out of the flat test: it is now looked for among all the
  symbol's orders since it was sent (`GetOrdersRequest(status=all, after=...)`), the account is not flat
  until it is found, and one never found is named in what is left. A node order in doubt is never taken
  for the check's close, and a close answered later ends the wait for a lost one, so an order the lost
  one becomes if Alpaca takes it late is canceled.

Code review (`/code-review`, high) of those fixes: the last reading did not look for a lost close, a
second close could be sent while the lost one might still be taken (two sells, a short), an answered node
order inside the search's span could be taken for the lost close, a failed read of the cache skipped the
node's disposal, the record named the node's own rejection as Alpaca's, the node's second local reason
(`NOT_FOUND_AT_VENUE`) was missed, and the docstring trailed the code; each fixed, the first three and the
sixth covered by a new test.

Four further independent reviews of the check (2026-11-10), each fix first shown by a failing test in the
same file unless named otherwise:

* The time left was judged before the start-up reads, so a window could start after its own end: it is
  judged again after them, and the window skipped if too little is left.
* At full request timeouts the 60 s ended before a close was sent: the deadline now gates the start of a
  poll, and a poll begun before it runs to its end, its close or cancels included.
* The lock's arithmetic left 0.4 s for a host clock behind Alpaca's, counted the close's own call nowhere,
  and its tests asserted the constants' own definitions: the bound counts the skew and a full poll, and a
  test measures the check against it.
* An order first seen in the reading after the deadline was never canceled: as the check ends, every
  order the last reading shows that the check did not send is canceled, and its own close too once
  Alpaca's clock says the session has closed (in the session it is left to fill).
* A heartbeat or record that could not be written replaced a refusal's own message: the refusal is said
  first.
* Once the session had closed, the cancels of the check's own closes had no per-order guard: every cancel
  now goes through one guarded step (no test of its own).
* A worker call still running after a clean stop was waited for twice, and the branch for a node that
  ended with its own error closed the runner with Python's 300 s wait: one wait, from the first shutdown,
  serves both, and that branch closes as after a clean stop.
* The reads after the clock were counted as adding to the lock's hold; they are inside the window's
  absolute end (a test shows the end the node is given). A blocked worker after a clean stop ends the
  process with the window recorded failed, and an order in doubt that Alpaca marks `done_for_day` is
  canceled as working (Alpaca: such an order "may be canceled through the API up until" filled, canceled
  or expired): both shown by a test that passes before and after.

Code review (`/code-review`, high) of those fixes: the final cancels took the check's own close, still to
fill in the session; the scheduled run's clock read was timed after its calendar read; a refusal whose
heartbeat failed lost its record; a failed last reading canceled again what had just been canceled; the
header and the bounds' notes trailed the code; each fixed, the first three covered by a new test.

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

* 2026-11-10: `paper:session`'s deadline moved from 85 to 105 minutes, so the start before the node and the
  window's bounded end both fit before the lock's owner is killed (ADR 0050, a second reader's review) (run).
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
* ADR 0037: `EmaCross` on Alpaca, the risk limits (now ADR 0049), the phantom reconciled position (read).
* The offline tests and the two paper starts above (run).
* Code review (`/code-review`, high), 2026-11-10: the naive calendar times, the ignored params, a second exit
  at stop or wind-down, a short counted as held, start-up eating the wind-down, an unwritten report;
  all fixed above. Second reader (`evidence-verifier`), 2026-11-10: corrected the cap's timing, the node
  log, "only a strategy" and the position check's retries; all applied.
* `crates/live/src/node/{mod,config,builder}.rs` and `crates/live/src/python/node.rs` at the pin: the
  node's stop bounds and their defaults, `with_delay_shutdown_secs` setting `timeout_shutdown`, and
  `run_async`'s cancel waiting for the same shutdown; `crates/trading/src/examples/strategies/ema_cross/`
  and `crates/trading/src/strategy/config.rs`: `EmaCross` unsubscribes on stop and nothing more, its base
  config exposes no `manage_stop`, and a market exit is reduce-only by default (fetched, raw).
* A second reader's review of `main` (2026-11-10), six findings, all reproduced offline and fixed here and
  in ADR 0047, since moved to ADR 0050 (run).
* alpaca-py 0.45.0 in `.venv`, `common/rest.py` and `common/constants.py`: `RESTClient._request` sends a
  request answered 429 or 504 again, `DEFAULT_RETRY_ATTEMPTS` 3 times, `DEFAULT_RETRY_WAIT_SECONDS` 3 apart,
  from the count it keeps at `_retry`; `TradingClient.__init__` passes no `retry_attempts` (`bundled`).
* Python 3.14's `asyncio` in the toolchain: `set_default_executor` takes a `ThreadPoolExecutor`,
  `to_thread` runs in the default executor, `Runner.close` waits for it at most 300 s (`THREAD_JOIN_TIMEOUT`);
  `concurrent.futures`' `shutdown(cancel_futures=True)` and `wait(timeout=...)` (read; a test runs both).
* Two more second readers' reviews of the window (2026-11-10): six findings, all reproduced offline and
  fixed above (run).
* `crates/live/src/node/mod.rs` at the pin: `LiveNode::stop` stops the trader, processes runner events
  until `delay_post_stop` (`process_runner_for`, a fixed deadline), then disconnects (`finalize_stop`);
  `crates/trading/src/strategy/mod.rs`: a managed `stop()` defers the strategy's stop until
  `finalize_market_exit` (fetched, raw). concepts/strategies, "Market exit": a managed stop "first performs
  a market exit, then stops the strategy once flat or once `market_exit_max_attempts` is reached".
* A third second reader's review of the window (2026-11-10): four findings beyond the six, reproduced
  offline and fixed above (run).
* alpaca-py 0.45.0 in `.venv`, `trading/client.py`: `close_position` returns "The order that was placed to
  close the position"; `get_order_by_client_id` returns an order by its client order ID (`bundled`).
  `packages/canon-alpaca/src/canon_alpaca/execution.py`, `_submit_order`: a submit whose answer is lost
  is logged as an unknown outcome and left submitted (read).
* An independent review of the check as landed (2026-11-10): two findings, reproduced offline and fixed
  above (run).
* `crates/live/src/execution/manager.rs` at the pin: a submitted order past its in-flight retries is
  rejected with `create_reconciliation_rejected(&order, Some("INFLIGHT_TIMEOUT"), ...)` unless the
  submission recovery policy is `RetainUnresolved`, and one its queries did not find with
  `Some("NOT_FOUND_AT_VENUE")` (fetched, raw). alpaca-py 0.45.0
  `GetOrdersRequest.after`: "only ones submitted after this timestamp" (`bundled`).
* Two independent reviews of the fixes as landed (2026-11-10): five findings, reproduced offline and
  fixed above (run).
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Order Lifecycle": the statuses after which
  "no further updates will occur", and "An order may be canceled through the API up until the point it
  reaches a state of either `filled`, `canceled`, or `expired`" (fetched, raw).
* Four further independent reviews of the check (2026-11-10): their findings reproduced offline and fixed
  above, or shown not to hold (run).
