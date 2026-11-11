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
dropped, never chased. The adapter sends that flag (ADR 0038). An extended window runs in pre-market,
after-hours or the overnight session (`EXTENDED_READY`, on once an overnight window passed on paper;
Confirmation), on the feed of the session's part, and skips a part no feed has been seen to serve on
paper, pre-market and after-hours so far (Sessions, feeds and what paper has shown).

**Each part, and the upstream it follows:**

| Part | Setting or code | Upstream it follows |
|---|---|---|
| Order size and rate | ADR 0049's `risk_limits`: $1,000 per order, 50 submits and 25 modifies a minute | `LiveRiskEngineConfig` (ADR 0049) |
| Orders and position in step with Alpaca | `open_check_interval_secs=10`, `position_check_interval_secs=60`, both off by default; the position check aligns to the adapter's position reports until `position_check_retries` (3) run out; since 2026-11-11 (claim 10), a submit never answered is retained (`submission_recovery_policy=RETAIN_UNRESOLVED`) and the strategy claims its instrument (`external_order_instrument_ids`), so a fill reconciliation books is its own | configure_live_trading, "Continuous reconciliation": "5-10s", "30-60s"; concepts/execution/reconciliation, "Submission recovery" and "Order ownership and tags" |
| Start-up reconciliation | Scoped to the symbol and one minute of history: every run starts only once it has found the account flat, so history can only mislead, and 60 minutes could hold the last window's exit without its entry; open orders and positions are reported however old | The Lighter tutorial's `reconciliation_instrument_ids`; the minute is the adapter's own paper tests' (the tutorial uses 60; the docs' tip leaves it unset) |
| The node | `LiveNode.builder(...)`, `with_risk_engine_config`, `with_exec_engine_config`, `with_delay_post_stop_secs(5)`; a regular window 65 s, since the managed stop's market exit (60 attempts a second apart) runs only inside that delay before the node disconnects | `examples/live/sandbox/exec_tester.py`, the Lighter tutorial; `LiveNode::stop` and `process_runner_for` in `crates/live/src/node/mod.rs` at the pin |
| Exit at wind-down and stop, in the session | `market_exit()`, and `manage_stop=True` so `stop()` runs it; `market_exit_reduce_only=False`, `market_exit_time_in_force=DAY` | concepts/strategies, "Market exit" |
| Start flat, confirm flat | Refuse to start unless flat in the symbol; after the node stops, read Alpaca and fail naming what is left (the account check, ADR 0060) | The Lighter tutorial: "Start with a flat target position", "confirm that no orders or positions from the run remain" |
| The session | Alpaca's clock and calendar before anything is placed | Alpaca's paper-trading skill: "always check the clock/calendar endpoint" |
| Bars | One-minute `INTERNAL` bars the data engine builds from trades; the adapter streams none | The data engine's bar aggregation |
| A position over one share | `PositionCap`, a non-trading strategy in the node (a `DataActor` has no portfolio at the pin): the portfolio's net position every second, `shutdown_system` over one share | No upstream: no setting caps a position, and the trading state is not settable from Python |
| A window's length and schedule | `minutes` (default 30, at most 60), clipped to the session's end and to `LONGEST_WINDOW_SECS` (Sharing the account), timed from Alpaca's clock; wind-down 120 s before; 300 s between windows; the session from configuration | No upstream |
| The extended session's parts | Pre-market and after-hours from the calendar day's `session_open` and `session_close`, refused when either is absent; overnight from 20:00 on the evening before a trade date on Alpaca's calendar to that date's `session_open`, so none before a weekend or holiday | Alpaca's 24/5 page: 8:00 PM - 4:00 AM ET, "on the evening before the trade date", "follows the NYSE holiday calendar" |
| The feed | `boats` overnight, IEX in the regular session, `sip` in pre-market and after-hours (below), the part deciding (each streams and serves bars; `sip` bars over REST seen on paper in the first pre-market window, ADR 0055), named in the strategy's quote and trade subscriptions, made before its bars, and its warm-up request | ADR 0055: params `{"feed": ...}`; the data engine hands the client an instrument's first subscription |
| Overnight eligibility | An overnight window starts only on an asset Alpaca marks `overnight_tradable` and not `overnight_halted`, read raw (alpaca-py 0.45.0's `Asset` has no such field), as a boolean (the 24/5 page's "boolean attributes") or a value of `attributes` (the asset reference; what paper showed), else refused by name | Alpaca's 24/5 page: "Assets tradable in the overnight session can be identified via the `overnight_tradable` attribute" |
| Data in the record | The quotes, trades and live bars that reached the strategy, and the adapter's own stream lines from the node's log; failed when the stream delivered no quote and no trade, or the adapter logged a stream error; a quiet market (quotes, no bar) is ok | No upstream: the adapter reports a refused or ended stream only in the log (ADR 0055) |

**Sessions, feeds and what paper has shown** (`session_window.SESSION_FEEDS`), and what each firing of the
schedule (ADR 0050, `--session auto`) does in each, New York time. A part where the feeds were probed on
paper and none delivered has no feed and is skipped, naming the probe and its date, as the lead decided on
2026-11-11, so the schedule does not fail every morning and evening by design; a part not yet probed runs, and its
windows are the probe that decides its row; inside a part with a feed, a window that receives no quote and
no trade fails (Data in the record). With no feed, `--session auto` says "nothing to run" and
`--session extended` "skipped"; both exit 0 and keep the last result. As of 2026-11-11 every part of a
trading day's sessions has a feed; on `sip`, after-hours and pre-market before 08:00 are not yet seen on
paper (UNVERIFIED), so their first windows are the probe.

| Part | Feed | Seen delivering on paper | A firing |
|---|---|---|---|
| Regular, while Alpaca's clock says open | IEX | Yes: upstream's data tester, 13:59 to 14:44, 2026-11-10 (the adapter's README) | Runs a window, market orders, ending `END_MARGIN_SECS` before the close |
| Pre-market, the day's `session_open` to the regular open | `sip` | Yes, 2026-11-11: the sip feed authenticated on paper on 2026-11-10, and authenticated and delivered on 2026-11-11; a read-only probe under `paper:test` (account A), 08:23:05 to 08:26:05 New York, received 1,167 SPY quotes, 240 trades and 3 bars on `sip`, where `iex`, 08:26 to 08:36, sent about 2,010 SPY quotes, 5 trades and 2 bars (QQQ: 7,292 quotes, 796 trades, 3 bars on `sip`; 1 trade on `iex`). Before that, `iex` and `boats` delivered nothing from 04:13 to 04:58, before IEX Exchange's 08:00 pre-market open: the IEX windows of 04:13 and 04:25 received no SPY quote or trade in 307.7 s and 1,506.4 s, and a probe at 04:53 none in 120 s on either. The first pre-market window, `SessionMaCross` on account B, `mise run paper:session --session extended --minutes 25`, 08:42:51 to 09:07:57 New York, 2026-11-11, exit 0, result ok: its warm-up was served 525 `sip` bars from the prior day's 15:35, so the averages were initialized at the start; 18,388 quotes, 1,571 trades and 25 live bars reached the strategy; it bought one share with a marked DAY limit at 757.10, filled at 757.05 at 08:49:00, and the wind-down sold it with a marked limit at 756.97, filled at 757.02 at 09:05:51; two fills, the cap's peak one share, no denial, nothing left | Runs a window, marked limits; its warm-up reaches back into the prior session (below) |
| After-hours, the regular close to the day's `session_close` | `sip` | Not yet on `sip`: `iex`, `overnight` and `boats` streams each sent no quote or trade in 45 s on four symbols at 16:46, 2026-11-10, though IEX's REST bars showed trades after the close (ADR 0055); after-hours runs on `sip` as pre-market does, as the lead decided on 2026-11-11, and its first windows are the probe | Runs a window, marked limits, warmed up on its own hour |
| Overnight, 20:00 on the evening before a trade date to its `session_open` | `boats` | Yes, 2026-11-10 (Confirmation) | Runs a window, marked limits, on an asset `overnight_tradable` and not `overnight_halted` |
| Friday 20:00 to Sunday 20:00, a holiday and the evening before it | None | | Nothing to run |

Pre-market and after-hours stream `sip`, as the lead decided on 2026-11-11, since `iex` sent too few
trades there to warm the averages up (the pre-market row; IEX Exchange's own pre-market opens only at 08:00,
"Pre-Market Session 8:00 a.m. - 9:30 a.m.", its trading hours page). The regular session stays on `iex`. A
part's feed is one setting in `SESSION_FEEDS`: a `sip` that Alpaca refuses or that delivers nothing fails the
window by name, and nothing switches to another feed. A pre-market window's warm-up starts `slow_period` plus
`WARM_UP_SPARE_BARS` bars, in the bar type's interval, before the last trading day's regular close on Alpaca's calendar
(`session_window.warm_up_start`, the calendar read a week back), as a span with no `limit`, through
`Strategy.request_bars`, which takes any `start`, and the adapter's bars request, which pages to the end; the
simple averages keep the last bars fed, so they start from the prior session's close and the after-hours and
pre-market bars since. Orders there are the extended session's marked DAY limits (Alpaca's orders page:
limit orders, `day` or `gtc`, with `extended_hours` true; `SessionMaCross._limit`). Known limit: if the
wind-down's limit does not fill, it is sent again only on a later bar, and the account check after the stop
sends no close outside the regular session (ADR 0060), so a share left is a failed window, named.

What a window knows at start-up, what is lost between windows, and the averages' warm-up: ADR 0047. The
engine's memory purge (`purge_closed_orders_interval_mins` and the rest, configure_live_trading, "Memory
management", off by default, for "long-running or HFT sessions") is not set: its recommended buffer, 60
minutes, outlasts a window, and the record reads the window's closed orders from the cache.

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
before its end runs on, at most two lookups, two reads, the clock and two cancels, seven requests; then
a last reading and its cancels, as many; about 481 s), about 725 s in all (`AFTER_END_SECS`); before its
start, 30 s of start-up and the first clock read, with alpaca-py's retries, 129 s; and the window is timed
on the host's clock, which may run 60 s behind Alpaca's (`SKEW_SECS`). So a window runs at most
3900 - 30 - 129 - 60 - 725, `LONGEST_WINDOW_SECS`, 2955 s (about 49 minutes); a `--minutes 60` window runs
that and its record says so (`shortened`). The sum leaves out uv's sync under the lock, for which
`paper:session` allows 600 s of start-up (ADR 0050 measured 8 s), so a test queued behind a window
outlasts it while that sync is short. The reads after the clock read are inside the window, whose end
is absolute, and a window whose start-up reads leave too little of it is skipped. Every request of the
check is counted (ADR 0060). The timeouts' sums are not wall-clock bounds (the connect timeout applies per address tried, the read timeout
between bytes, ADR 0039). A window also ends `END_MARGIN_SECS` (515 s) before its session: the check's last
market close goes out at most about 455 s after the end (`LAST_CLOSE_SECS`), and a minute covers the skew,
so it is sent while the session is open. The same sum with `paper:session`'s 900 s wait for the lock fits its 105-minute
deadline. A test reads the 3900, the 900 and the 105 minutes from `mise.toml`.

**The account check after the stop** is [ADR 0060](0060-session-window-account-check.md): which requests a
window does not yet know the end of, how it closes what is left, and how its requests are counted.

### Consequences

* Good: a whole session is a sequence of bounded, checked windows on the engine's own reconciliation.
* Bad: a paper test can lose the race for the lock to the next window. What replaces the sharing is a second
  paper account for session runs, as ADR 0010's last step already asks for separate accounts; that is the
  owner's to create.
* Bad: with extended windows on, the schedule (ADR 0050) fires a 25-minute window every 35 minutes from
  04:00 to 20:00 on a trading day and through the overnight session before one, so from Sunday 20:00 to
  Friday 20:00 except a holiday and the evening before it; a half day's after-hours ends at its earlier
  `session_close`. Firings in pre-market and after-hours skip, since neither has a feed (the table
  above), so windows run in the regular session and overnight only, and the figures below for how long
  the lock is held apply to those hours; in pre-market and after-hours no window holds it. A window holds the paper lock for its 1,500 s, the
  start-up before it and the stop and check after it: the overnight window above recorded 906 s for its
  planned 900, its start-up not counted (`START_UP_SECS` allows 30; UNVERIFIED), and a window whose stop
  or check runs to its bound holds it up to `AFTER_END_SECS` (725 s) longer. So the lock is held about
  26 of every 35 minutes, and is free for about 9. A `paper:test` that queues at a random moment waits about 10 minutes on average and about 27 at most
  (about 38 at the bounds: 30 + 1,500 + 725 s; and up to about 50 with the start-up's sync, at most 600 s,
  and the first clock read, 129 s), inside its 3,900 s wait; its output shows nothing until the lock is taken, and past
  3,900 s it prints "paper:test: the paper lock stayed held for 3900 s; nothing was placed" and exits 1.
  A test or a window by hand that holds the lock past the next firing's 900 s wait makes that firing
  give up: `paper:session` prints "the paper lock stayed held for 900 s; nothing was placed" to the
  runner's log and exits 1, before Python runs, so neither `latest.json` nor `last-firing.json` changes,
  and `scripts/automation status` shows the paper session FAILED, "the last firing exited 1", until a
  later firing runs. So a paper test of over about 15 minutes, started in the 9-minute gap, costs the
  next window.
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
are set; the window refuses an extended session while `EXTENDED_READY` is off, outside pre-market,
after-hours and an overnight session before a trade date, the regular session when closed, a non-trading day, a calendar day without `session_open` or `session_close`,
over 60 minutes, an account not flat and a start inside the gap; on macOS, a run with no
`--log-dir` whose `getconf DARWIN_USER_TEMP_DIR` gives no absolute folder, and a relative `--log-dir`,
are refused before Alpaca is read.

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
  dropped once. A cancel that went unanswered leaves it pending cancel, which the strategy cannot cancel
  again (refused at the pin); the node's in-flight check (which queries it, and the adapter answers
  `QueryOrder`), its open-order check and the window's account check, which cancels in any session, are
  what clear it. Extended
  windows, the only ones that send these limits, were then off (`EXTENDED_READY`).
* A close that filled in part and timed out held the dropped side, so the shares left stayed open for the
  rest of the cross: a limit that filled in part, by its timeout or while its cancel was on its way, now
  drops nothing.

Code review (`/code-review`, high) of those fixes: a cancel sent again for an order pending cancel sends
nothing (above), a part fill racing the cancel, a part-filled limit reported as a dropped signal, a
heartbeat whose own write failure hid the run's error, the delay set by a second builder call, and
`Runner.close` waiting up to 300 s for a worker call after a clean stop (now at most `ADAPTER_CALL_SECS`,
then the runner is left as after an abandoned stop); each fixed, the first two and the last covered by a
new test.

The reviews of the account check from 2026-11-10 on, and what each changed, are in ADR 0060.

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

On paper, 2026-11-10, in the overnight session, `mise run paper:session --session extended --minutes 15`,
by hand, 23:00:53 to 23:15:58 New York, exit 0: the account flat at the start; SPY `overnight_tradable`,
read from its asset's `attributes`; the part `overnight`, the feed `boats`; the warm-up gave 26 `boats` bars
and initialized the averages; 3,700 quotes, 120 trades and 14 live bars reached the strategy, with no
stream line from the adapter. A cross sent a DAY limit buy, marked by the strategy's code (the log does
not show the flag; an unmarked order would have queued, not filled overnight), of 1 SPY at 754.58, accepted and
filled at 754.53; the wind-down's marked DAY limit sell at 754.37 filled at 754.42; the record `ok`, nothing
left, no close after the stop, the cap's peak one share. Two 10-minute windows the same night, the second
on the window's code as landed (after the code review below and a rebase; the adapter's own changes landed
in between were not run in a window): 23:23:46 to 23:33:55, 25 warm-up bars,
2,710 quotes, 25 trades, 9 live bars, a cross's buy filled at 754.39 and the cross back's sell at 754.42;
23:39:35 to 23:49:41, 25 warm-up bars, 1,552 quotes, 38 trades, 9 live bars, a buy filled at 754.64 and
the wind-down's sell at 754.64; each `ok`, exit 0, nothing left, no stream line. `EXTENDED_READY` was turned
on with them; no pre-market or after-hours window had run on paper then (the pre-market windows of
2026-11-11 are in the table above).

2026-11-11, a correction to the warm-up: until then `SessionMaCross.on_historical_bars` fed the history to
the averages a second time, after upstream's response path had already fed them (`prepare_response`,
`crates/common/src/python/response.rs` lines 357-363 at `d53849b216e2`, which routes a bar by instrument
and bar specification whatever its aggregation source, `BarType::id_spec_key`). With fewer history bars
than `slow_period` the duplicates made the slow average report itself initialized on a doubled window, and
the first live bar decided on it; the callback now only counts the bars, shown first by a failing test in
`tests/test_session_window.py` on the `BacktestEngine` harness (five history bars against a slow period of
eight). The windows recorded here were not affected: each was served at least `slow_period` (20) bars,
the first pre-market window 525, the overnight windows 25 and 26, and a simple average of period n holds
only the last n bars it was fed. The second feed was the same bars again, oldest first, so its last 20 (and
the fast average's last 10) were the bars the first feed had ended on, and the averages' values at the
first live bar were the same as with one feed. None of their answers was late: the old callback counted
a late answer's bars as dropped, not as warm-up bars, and each window's warm-up bars were its whole
answer. A late or out-of-order answer is now fed again in time order with the live bars (the module
header of `session_ma_cross`).

Code review (`/code-review`, high) of the extended windows: ten findings, each fixed: the market tuple's
type; the node's log read before its writer had flushed (now after `logging_sync_to_disk`); the record
claiming a paper run not yet written; a refused subscription ("Cannot subscribe") read as a quiet market;
the level matched anywhere in a line (now the line's own field); an overnight session refused for a bound
it does not need; an unreadable asset failing rather than refusing; a `--feed` that could name a feed the
part does not serve (removed: the part decides); subscriptions sent before `MaCross`'s instrument check;
and leftovers (a stale refusal message, a function with no caller, two ways to say no feed). The last seven
are covered by a test.

2026-11-11, ADR 0068's step 3: a window takes a strategy by name from a fixed table
(`session_window.WINDOW_STRATEGIES`, `--strategy` through `paper:session` and the runner, `SessionMaCross`
by default, so the schedule is unchanged), and the name is in its record and the `scripts/automation
status` row. Upstream's built-in `EmaCross` is the other entry, added through `add_builtin_strategy` beside
the cap; it does not flatten on stop and the account check closes at market in the regular session only,
so a named extended session is refused for it by name, `--session auto` outside the regular session has
nothing to run for it and says why (exit 0), and a part whose feed is not the adapter's default is
refused, since its subscription names none. It has no wind-down: it trades to the stop, and an order it
sent then is the account check's. The decision above is unchanged: no setting makes `EmaCross` safe
unattended, and a window of it is a bounded check. At the pin it reads neither its position nor its
working orders, and an order refused still flips its side, so a second order can stack a position (ADR
0037's 4-share short); so its window's submit budget is one order (`EMA_CROSS_SUBMIT_RATE`, one an hour,
longer than any window), ADR 0037's bound through the risk engine's own rate limit, which denies every
later cross before it leaves the node. The cap stays as the backstop and the account check closes the
share. Offline, `tests/test_session_window.py`: the table and its refusals, the node's wiring and budget,
one order sent and every later cross denied over an hour of quotes, the record's strategy, and a
built-in's orders and quotes read from the node's cache (run); a built-in's quote and trade counts are
the cache's, at most its `tick_capacity` (10,000), so they show that data came, not how much. Code review
(`/code-review`, high), 2026-11-11: ten findings. The one that changed behaviour, `--session auto`
resolving to the extended session whatever the strategy (exit 1 where nothing was to run), was first
shown by a failing test; the refusal's wording now comes from the table, the node is built from the
table, the strategy ID is required where orders are read, and the status row's strategy is in its
self-test; the capped count and the missing wind-down are stated above; a feed check that the table
makes unreachable is kept as a guard. On paper: pending, the first regular window of it (ADR 0068,
Confirmation).

2026-11-11, the session window's claim 10 (a fourth independent review, 2026-11-10): a submit Alpaca took
whose answer and every read of the order were lost was rejected by the node itself once the in-flight
check's queries ran out (`INFLIGHT_TIMEOUT`, `check_inflight_orders` in `crates/live/src/execution/manager.rs`
at the pin), and the fill Alpaca made afterwards was booked to `EXTERNAL`: the adapter ignores an update for
an order that has ended, and the position check's fill report, under an Alpaca order ID the node never
heard, became an external reduce-only order (`materialize_external_order_from_fill`,
`crates/execution/src/engine/mod.rs`), then the check's own `RECONCILIATION` order. `SessionMaCross` reads
its own positions (`held`), so after a closing sell it would sell a share it no longer held, and after an
entry buy a second. Reproduced offline on a real `LiveNode` with the adapter on ADR 0075's model and
stand-ins for Alpaca (`packages/canon-alpaca/tests/test_execution_client.py`), for an entry and a closing
sell, the trade update delivered or lost. Two upstream settings close it, and each is needed:

* The paper nodes' engine retains such a submit (`submission_recovery_policy=RETAIN_UNRESOLVED`,
  `paper_node.exec_engine_config`; concepts/execution/reconciliation, "Submission recovery": "Exhausted
  unacknowledged submissions stay unresolved"), so the order stays open at the node and the strategy, which
  sends nothing while an order works, sends no new order of its own. Claimed but resolved locally, the order was closed
  with nothing held while Alpaca held it.
* The window's strategy claims its instrument (`external_order_instrument_ids`; "Order ownership and tags":
  "Claimed orders and fills use the claiming strategy ID"), as 14 of the 15 upstream
  `examples/live/*/exec_tester.py` claim theirs (all but Interactive Brokers'). Retained but unclaimed, a lost update's fill still went to `EXTERNAL`.

With both, the strategy's position was Alpaca's in all four cases. Delivered, the order filled under
Alpaca's trade ID. Lost, it stayed submitted and the node's run ended with the error naming it ("Submission
recovery at shutdown", concepts/live), so the window is recorded failed and its account check looks the
order up. An update delivered after the position check had booked the fill was not counted again (observed
while building, not kept as a test). The window's tests of the claim and of the engine's policy
(`tests/test_session_window.py`) failed before the change. The case needs a submit with no answer (a
5xx or a read timeout, ADR 0039) and every read of the order failing through the in-flight check, which
no paper run has shown: it is UNVERIFIED on paper.

What the retention costs, from the code review of this change: a read that finds no order is no outcome,
so a submit answered 5xx that Alpaca never placed is retained too, and the strategy sends nothing more for
the rest of the window, which is then recorded failed
(`test_a_submit_alpaca_never_placed_is_retained_too_and_nothing_is_sent_again`); Alpaca asks that a
timed-out order not be marked canceled "until confirmed" (ADR 0039's Evidence). Any submit still
unanswered at the end of `delay_post_stop` fails the run, its recovery budget spent or not
("Submission recovery at shutdown"), for the EMA paper node as for the window. Retention does not stop a
market exit: at the wind-down and again at the managed stop, `market_exit` cancels the strategy's orders
and closes its position as the node holds it (`market_exit`, `crates/trading/src/strategy/mod.rs` at the
pin), so a retained close whose fill the position check has not yet booked can be sent a second time, as
a close rejected locally could before; the account check after the stop finds and closes what that
leaves, in the regular session.

The limit left: with the trade update lost, the retained order stays `SUBMITTED` until the node stops, which
then fails with "Submission recovery incomplete at shutdown: unresolved client order IDs [...]" naming it,
so the window is recorded failed; the account check after the stop looks the order up by its client order
ID and, in the regular session, closes the position it left. The adapter could resolve the order instead
by setting the order's client order ID on its fill reports, so the engine matches the fill to it, as
upstream's OKX adapter sets the one the venue echoes (`parse_fill_report` in
`crates/adapters/okx/src/common/parse.rs` at the pin); this adapter's fill reports leave it unset, and
that change to the adapter's fill path is not built here.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* 2026-11-11, the warm-up fed once: NautilusTrader `crates/common/src/python/response.rs` lines 357-363,
  `crates/common/src/actor/indicators.rs` lines 171-178 and 241-246, `crates/model/src/data/bar.rs` lines
  910-921 at `d53849b216e2` (read in the local copy at the pin); `SimpleMovingAverage.reset` in `.venv`
  (`bundled`). Offline, `tests/test_session_window.py` on the `BacktestEngine` harness: thin and full
  history fed once, and an answer after a live bar fed again in time order with the live bars; directly,
  an answer out of order, with a bar twice, overlapping the live bars, or older than the bars taken (run).
* ADR 0069, 2026-11-10: a second paper account, B, for session windows. It amends "Sharing the account"
  once `paper:session` reads B's pair, with a lock of its own; until then this record stands as written.
* 2026-11-11: pre-market and after-hours given `sip`, as the lead decided that day, with no fallback to
  another feed; a pre-market window's warm-up reaches back into the prior session. Sources:
  https://www.iex.io/resources/trading/trading-hours-holidays, "Pre-Market Session 8:00 a.m. - 9:30 a.m."
  (fetched, summarised); https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, pre-market "4:00am -
  9:30am ET", "Only limit orders with `time_in_force` set to `day` or `gtc`", the `extended_hours` boolean
  (fetched, summarised); NautilusTrader `crates/common/src/python/actor.rs` line 2956 at `d53849b216e2`
  (https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e2/crates/common/src/python/actor.rs,
  `request_bars` with `start`, `end`, `limit`; read in the local copy at the pin); alpaca-py
  `alpaca/data/requests.py` line 39 at `c42add7c0a70`
  (https://github.com/alpacahq/alpaca-py/blob/c42add7c0a70/alpaca/data/requests.py, bars sorted ascending
  by default; read in the local copy at the pin). The probe under `paper:test` and the first window, its
  row (run). Offline, `tests/test_session_window.py`: the feed per part, a refused or empty `sip` failing
  the window by name, the warm-up's start from the calendar in the bar type's interval, a calendar with no
  prior trading day refused, and the reach-back across a 15-hour gap in a backtest (run). Code review
  (`/code-review`, high): a refusal raised as a failure and a span counted in minutes, each fixed with a
  test; stale docstrings corrected; `sip` history for the last 15 minutes doubted, and served on paper.
* 2026-11-11: pre-market joined after-hours as a part with no feed, on the observations in its row of the
  table: the two windows' records (read), the probe under `mise run paper:test`, read-only, SPY through the
  adapter's node on `iex` and `boats` and alpaca-py's REST bars on each, 04:53 to 04:58 New York, exit 0
  (run), and https://docs.alpaca.markets/us/docs/paper-trading.md, "only entitled to receive and make use
  of IEX market data", with https://docs.alpaca.markets/us/docs/historical-stock-data-1.md, `iex` "the only
  feed that can be used without a subscription" (fetched, raw). Offline, `tests/test_session_window.py`: a
  pre-market firing exits 0, builds no node, keeps the last result and names the observations (run).
* 2026-11-11: the lead decided that a part of a session no feed has been seen to serve on paper is skipped
  by name, with the observation and its date, rather than run to fail; after-hours is such a part, on ADR
  0055's observation of 2026-11-10 (read). Offline, `tests/test_session_window.py`: an after-hours firing
  exits 0, builds no node, keeps the last result and says so in its heartbeat (run).
* 2026-11-11, the second reader (`evidence-verifier`) of the split: corrected here the refusals listed in
  Confirmation (no SIP refusal remains), the data plan behind `boats` marked UNVERIFIED, the two forms of
  the overnight attributes stated, the marking of the overnight orders read from the code, what of the
  landed code the last window ran, the lock's bounds (about 38 minutes, 50 with the start-up's sync), the
  days without windows, and the sync the window's length leaves out (read).
* 2026-11-11: the account check after the stop, its reviews from 2026-11-10 on and the sources only they
  cite moved to ADR 0060 with no change of decision; the second reader's correction of 2026-11-10 and the
  Lighter tutorial stay here too, since text here rests on them. Corrected: the feed row named a `--feed`
  that was removed before the extended windows landed (read).
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
* 2026-11-10, a second reader's correction (four independent reviews of the check, twelve claims on the
  window, all reproduced offline): the decision's account check, its row in the table and the lock's arithmetic are
  rewritten above; `AFTER_END_SECS` 605 to 725, `LAST_CLOSE_SECS` 394 to 455, `END_MARGIN_SECS` 455 to
  515, `LONGEST_WINDOW_SECS` 3076 to 2955 (run).
* 2026-11-10, second readers' sweep for silent fallbacks (Muse Code, DeepSeek Harness), each reproduced
  by a failing test: a calendar day without `session_close` (`Optional[str]` in alpaca-py 0.45.0's
  `Calendar`) gave an extended window a 20:00 end, and a failing `getconf` on macOS moved the records to
  `$TMPDIR`'s folder; both are now refused by name, as is a relative `--log-dir` (what `mise.toml`'s
  `paper:session` passes when its own `getconf` prints nothing; its lock file has the same gap). The window's code no longer matches a close by its
  quantity, so the third claim, an unreadable `qty` taken for no close, no longer applies. Corrected: the
  adapter answers `QueryOrder` (ADR 0036), so the in-flight check queries a limit left pending cancel (run).
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md: the overnight session's hours "on the
  evening before the trade date", the NYSE holiday calendar, the `boats` and `overnight` feeds,
  `overnight_tradable` and `overnight_halted` (fetched, raw, read whole).
  https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id.md: the two as values of
  `attributes` (fetched, raw); on paper SPY's asset carried `overnight_tradable` there.
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md, "Memory management": the
  purge settings, off by default, a 60-minute buffer recommended (fetched, raw).
  https://nautilustrader.io/docs/md/latest/concepts/logging.md: `FileWriterConfig.file_name`, plain text
  with a `.log` extension (fetched, raw). nautilus-trader 2.0.0rc8 in `.venv`: `Strategy.on_quote`,
  `on_trade`, `subscribe_quotes` and `subscribe_trades` with `params`, `logging_sync_to_disk` (`bundled`).
  alpaca-py 0.45.0 in `.venv`: `RESTClient.get`, the call `get_asset` makes; `Asset` without
  `overnight_tradable` (`bundled`).
* The overnight paper window above (run).
