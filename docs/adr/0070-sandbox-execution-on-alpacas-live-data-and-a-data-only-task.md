---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Simulation without the paper account: NautilusTrader's sandbox execution client on Alpaca's live data, and a data-only task that cannot trade

## Context and Problem Statement

The owner asked on 2026-11-10 whether simulation could run without limit on NautilusTrader's native
features. ADR 0068
lays out the lanes; the backtest forms are there. This record covers the third native form, the sandbox
execution client, which matches orders locally on live market data: whether it works here at the pin, what
it models, how many strategies it carries, and what a process that only streams would need so that it
cannot place an order.

## Decision Drivers

* Upstream's form as documented (`examples/live/sandbox/exec_tester.py`), configured only.
* No order reaches Alpaca from a sandbox run, held in code, not by intent (ADR 0009); until the task
  is built, by how the node is built and a check after the build (term 6).
* The paper pair reaches a process only on ADR 0029's terms.

## Considered Options

1. Sandbox runs through `paper:test`, under the paper lock, as the proof below ran.
2. A data-only task, `paper:data`, that gives the pair to a process that streams or reads and never
   trades, with "never trades" failing closed in code.
3. A key or token that cannot trade, held at Alpaca.

## Decision Outcome

Chosen: **option 2**, under the owner's delegation of 2026-11-10; option 1 served until it was built
(2026-11-11, Confirmation), after the reading side of ADR 0069. Option 3 is not shown for paper keys (below). What is untested is UNVERIFIED.

**The sandbox at the pin.** `add_simulated_exec_client(name, SandboxExecutionClientFactory(),
SandboxExecutionClientConfig(venue=...))` on a `LiveNode`, reachable from Python. The client matches in
the `OrderMatchingEngine` on the quotes, trades and bars the bus carries for its venue (`execution.rs`,
`register_message_handlers`), so on venue `ALPACA` it fills on the Alpaca data client's live data: proved
once (Confirmation). Fill, fee, latency and margin models, queue position and liquidity consumption are
its, off unless set (`config.rs`); Alpaca's rules are not: the adapter's denials, the 403 wash-trade
refusal ("potential wash trade detected", as `test_paper_matrix.py` records it), paper's random
partial fills ("Paper Trading"). A sandbox run is comparable to paper only for
orders the adapter would pass. NautilusTrader v2.0.0rc9 fixes the sandbox's open-order check rejecting
resting orders and GTD fills after expiry (release notes), so an unattended sandbox lane waits for that
pin move through `adopt-upstream`.

**Many strategies, one node, one stream.** `NETTING` keeps "one position per instrument and strategy", ID
`{instrument_id}-{strategy_id}` (`positions.md`), and the sandbox reports no positions ("Position reports
are always empty"), so nothing merges them. They share the one simulated account's cash, and each needs
its own `order_id_tag` (refused otherwise; run). They share one data client and one stream, within the
plan's symbols (Basic: 30, "About Market Data API"); Alpaca allows "in many subscriptions (or without
one)" one connection, 406 beyond ("Streaming Market Data"), so parallel sandbox nodes need a stream each
and parallel strategies go in one node. A built-in subscribes with no params, so it streams IEX (ADR 0055);
overnight it needs a strategy that names `boats`, one night's observation, not an entitlement (ADR 0055).

**The pair, the lock and the trading host.** The data client authenticates with the paper pair, which
reached only `paper:test` and `paper:session` when this was decided (ADR 0029, ADR 0044), so a sandbox run
then held the paper lock; `paper:data` now gives it account A's pair on the terms below. Its instrument provider builds a `TradingClient` for the asset reads (`providers.py`), and
`/v2/assets` is documented on the trading hosts only, so a data-only process holds a pair that can trade.

**The data-only task's terms.**

1. `paper:data` reads the pair the tests use (today's, service `canon-alpaca-paper`) into its own
   environment, as `paper:test` does (ADR 0029), and by no other route.
2. It runs one named module, never a test runner over a path. Two modules are allowed; a third needs a
   new record:
   * the sandbox node, which holds the stream lock ADR 0069 defines (whether Alpaca's limit spans a
     second paper account of one login is UNVERIFIED);
   * the historical quote loader ADR 0066 proposes, once built: REST reads only, no stream and so no stream lock, at a rate
     that leaves room for the windows' own data calls once that share is measured (UNVERIFIED until then).
3. "Never places an order" fails closed in code, twice: a transport adapter refusing every method but GET
   before anything is sent, on every alpaca-py REST client the process builds. It is part of the adapter
   `providers.with_http_timeout` mounts (a mode of `TimeoutAdapter`), not a second one: that function
   replaces any other adapter each time it runs, and the provider runs it on every `_client()` call, so a
   separate refusing adapter would come off silently (shown offline by a second reader). And a check, after the node is built, that no Alpaca
   execution client is registered, failing the run.
4. Its test shows `submit_order` (POST), `replace_order_by_id` (PATCH), `cancel_order_by_id` and
   `close_position` (DELETE) each refused with nothing sent, after the provider's client is fetched again,
   and a node carrying an Alpaca execution client refused.
5. It guards this repository's code paths, not any code holding the pair. It lets a process read assets,
   stream quotes and trades and request historical data; it does not let one submit, replace, cancel or
   close.
6. Until it is built, a sandbox run under `paper:test` checks after the build that no Alpaca execution
   client is registered and fails otherwise, as the proof's probe did.

**Addition, 2026-11-11: two listed modules for the movers probe.** Decided by the lead under ADR 0074 on
the owner's instruction of 2026-11-11: term 2's "a third needs a new record" is relaxed, and a further
data-only module joins the list by an ordinary reviewed change with its GET-only test and a dated line
here. The list is now `sandbox`, `movers-stream`, `movers-screener` and `quotes`. `movers-stream`
(`canon_trading.movers_stream`) runs alpaca-py's `StockDataStream` on `sip` with raw messages, bars `*`
and then trades `*`, and its `StockHistoricalDataClient` for snapshots and minute bars. Each minute it logs
message counts, peak memory and the top 50 by the movers ranking score. It runs 1 to 60 minutes and holds
the stream lock. `movers-screener` (`canon_trading.movers_screener`) reads alpaca-py's `ScreenerClient`
(`get_market_movers`, and `get_most_actives` by volume and by trades) once a minute, for 1 to 85 polls,
and takes no stream lock. Only modules that stream now take that lock, and `paper:data`'s timeout is 100
minutes so the screener's longest run fits. Both modules keep `CANON_ALPACA_DATA_ONLY=1`, pass their one
REST client through `with_http_timeout` and `require_get_only`, import nothing from `alpaca.trading`,
and log to `canon-paper-data/` in the per-user temporary folder. Their tests show POST, PUT, PATCH and
DELETE on each module's client refused with nothing sent, no `TradingClient` built, and the screener run
without the stream lock (`tests/test_movers_*.py`, `scripts/test-lanes`, offline). The ranking score has
no upstream, so its arithmetic is in the module header, and its parameters are UNCALIBRATED. Whether one
`sip` connection carries `*` for bars and trades, and at what rate, is UNVERIFIED until a run measures it.

**Addition, 2026-11-11: the scheduled movers recorder.** On the owner's weekday-window instruction,
`scripts/automation install` adds `local.canon.movers-recorder`, reported by `status`, compared by `differs`
and removed by `uninstall`. It runs the existing `paper:data movers-screener` choice, not another module.
Launchd's `StartInterval` is 60 seconds, not at load; while a loop runs it misses those firings
(`launchd.plist(5)`). The wrapper gates every firing with `TZ=America/New_York date`, independent of the
machine's zone, and runs back-to-back batches on weekdays from 04:00 inclusive to 20:00 exclusive.
Each batch is at most 80 polls, as the by-hand loop used, below the module's 85-poll bound; the last batch
is shortened to fit the time remaining after startup and HTTP headroom, and `timeout(1)` plus mise's
`run --timeout` bound the whole invocation by the close. The hand loop's extra five minutes after 20:00
are not part of this window. Weekday holidays and early-close afternoons follow the same policy: no
market calendar is consulted. The weekday-holiday test uses
2026-11-26 (Thursday); the screener's actual holiday responses remain UNVERIFIED.

The installed copy of `scripts/automation` runs its `record-movers` command and installs a private
`movers-checkout.sh` copy of the existing `paper-session.sh --checkout-only` contract, independently of
the paper agent's installer or deferral, for its own `~/.claude/canon/movers-checkout` clone of
origin's `main`, refreshed before each batch. Its recorder-only `--recorder-checkout` mode reuses a matching
marked clone, following actions/checkout's existing-directory contract; the paper job and installer keep
the fresh `--checkout-only` contract. It takes no account or stream lock: the screener reads REST
only. Logs remain in the per-user temporary folder outside the clone; its status record under
`~/.claude/canon/watch/` names the commit, outcome and exit code. **Review amendment, 2026-11-11:** the
Claude lane's medium-effort review of c10 (`37b986e`) found that a transient failure latched the whole
New York day, contrary to the owner's automatic recovery requirement after sleep and network loss.
The lead replaced that latch: a failed batch or setup ends the current firing by name; the next firing
starts fresh on the same date. `RECORDER_FAILURE_BOUND=3` is UNCALIBRATED: the consecutive failed-firing
counter saturates at three and writes `consecutive_failed_firings_bound (UNCALIBRATED)` in the status
row, without suppressing another attempt. A firing clears that count only after completing at least
one batch successfully and ending without a failure. A zero-batch close gap or outside-window firing
keeps the prior failed status and count, without preventing the next eligible firing from trying.

On the coordinator's clarification of the same review, a failed per-batch fetch is the exception:
if a marked clone matching the origin has a last good commit in `refs/remotes/origin/main`, restore
and clean that checkout, retain `.venv` (removing a symlink without touching its target), run its batch
and record `fetch_failed, ran at <short sha>`. A local HEAD commit is not the cached main commit.
Without such a commit,
end the firing with `checkout_failed`. Both checkout-lock acquisitions wait up to
`RECORDER_LOCK_SECS=60` each (UNCALIBRATED), trying between one-second sleeps for a leftover uv to exit;
`checkout_lock_timeout` ends the firing when the bound expires. A held recorder/installer lock records
`recorder_lock_held` and also ends only that firing; outside the window the firing ends idle instead,
since there is nothing to record and maintenance's `mise self-update` holds that lock by design. A failure reading New York time still writes
`time_failed` with a fresh epoch from jq's `now`, marking the time source and preserving the last known
New York day, or `unknown` if none was read. Uninstall preserves the recorded data and status history.
Each batch starts with fresh commit and fetch metadata; a prepared checkout is not recorded as code
that ran until the batch is starting. Before that, a failed fetch is named `fetch_failed, checkout at`.
The landing lane's medium review of round two (2026-11-11) left as limits: a full 80-poll batch's
budget (6,058 s) exceeds `paper:data`'s own `deadline=5940`, so a batch slowed that far ends by the
task's bound as `batch_failed`; a signal during the checkout helper leaves the record `running`; a
deferred recorder install reruns the whole install each maintenance round until the loop ends; firings
outside the window still rewrite the status every minute.

Installation and the loop share the recorder's exclusive lock (`lockf(1)`), held for the entire loop,
including the gaps between batches. A running loop defers replacement of its runner and plist, the
preflight included, to a later maintenance round (ADR 0065); installation holds the same lock through
preflight and reload, so a firing during installation records its named lock failure and can retry.
`scripts/automation recorder-lock-path` supplies that lock's path to maintenance, instead of a second
hard-coded path. The recorder owns its private installed `movers-checkout.sh`; it does not depend on
the paper job's installed `paper-session.sh`, which uninstall removes when the paper job is stopped,
even if the recorder refuses to unload.
Within a firing, only the wrapper holds the recorder's loop lock; checkout helpers and batch children close its
descriptor 9. Batch descendants inherit the separate checkout lock, so a leftover uv causes the next
firing to wait for that lock within its named bound. TERM, HUP or INT to the wrapper forwards TERM to
the batch's timeout and waits for it before exiting with a named `firing_interrupted` status. The local
timeout contract forwards that signal to its group, and pinned mise forwards it to its task groups;
`paper:data`'s existing trap kills its uv group. This also stops a batch on uninstall instead of leaving
it detached from the unloaded job.
Maintenance records this explicit deferral as unfinished work and retries at its next round instead of
caching it as a completed install for a day. This can defer a recorder update until the current loop
ends, normally at 20:00 or at its first failed batch; other automation still installs. A stale record
keeps the STALE label when the installed runner is also outdated, using the paper row's severity order.

The strict close is retained on the deputy's reversible decision of 2026-11-11, reported to the lead.
The final batch reserves 300 seconds for mise's start (the keychain reads included), the task's 600-second
environment sync, the module's 30-second startup allowance and 388 seconds for its final three REST reads
(three times 129.2 seconds at the adapter's timeouts and retries, rounded up). It also leaves 60 seconds
for launch and cleanup. Polls start immediately then wait one minute each; only the polls fitting that
budget run, capped at 80. Mise's global timeout receives the run budget plus 5 seconds; its documented
command-tree termination has a 5-second grace. The outer timeout sends TERM at the budget plus 30 seconds
and KILL 5 seconds later, before 20:00. No fitting poll, or the end of a shortened final batch, records
a named `recording gap` with its start and 20:00 end in New York time, in the runner's log and status.
This conservative reservation can leave the last part of the data window uncaptured; it adds no grace
after the close. The caller is bounded even when a read exceeds its per-address or between-byte timeout.

Installer preflight runs only after the idle job is unloaded; a refused unload leaves the runner, helper,
plist and clone untouched. An invalid or unavailable origin removes the stopped job's old plist.
Status reads the directory named in that plist and ignores launchd's nonnumeric `(never exited)` marker.
Every setup failure is recorded with a fresh timestamp and ends that firing. The mise ceiling is
the physical checkout parent and rejects list separators. Uninstall reports a failed mise untrust while
still removing its stopped clone, and names the preserved `canon-paper-data` directory. Mise's own pin
update shares the recorder lock and waits for its loop to end. Screener filenames include the process ID
(`os.getpid()`), keeping concurrent scheduled and manual runs in distinct files even in the same second.

Verified offline in `scripts/test-maintenance`: a simulated bootout during a batch loses its failure
before the lock fix; afterward installation leaves the wrapper alive to record the failure and end
the firing. Antigravity's round-2 review (2026-11-11, `xr-agy-c10-r2/REPORT.md`) found zero-batch status
clearing, detached batches on stop, inherited loop locks, stale or premature run metadata, cached HEAD
provenance and a missing setup-failure record; all six have failing-before offline regressions.
The reverse preflight race, maintenance retries, weekday holiday, status order and document
listing have failing-before cases. Actual launchd scheduling, a real paper-data run and real installation
remain UNVERIFIED; the coordinator reproduces and reviews, and maintenance installs from `main`.

The next step after the transport refusal, not built now: instruments read from the catalog would keep the
process off the trading host altogether, an adapter change.

**Option 3.** Held at Alpaca it would be stronger, but the key access levels read are the Broker API's
(credential management); an OAuth token "assumes" read-only access by default and adds `trading` only when
asked, and whether a `data`-only token reads `/v2/assets` is UNVERIFIED.

### Consequences

* Good: live-data runs with no account and many strategies on one stream; the proof ran on `main`'s code.
* Good: a data-only process cannot send an order through this repository's clients: two checks in code,
  shown offline by term 4's test (Confirmation).
* Bad, updated 2026-11-11: a sandbox or `movers-stream` run holds A's stream lock for its run, up to
  60 minutes plus start-up and stop. Another streaming `paper:data` task waits at most 900 s for that
  lock; if it stays held, the waiter exits 1 without running its module while the holder continues.
  Windows on B use B's stream lock and no longer wait for these data runs (ADR 0069). The data task's
  headless deadline is 5,940 s, with a 100-minute outer timeout; a streaming paper test's at-most-3,900 s wait can run out
  behind it. That wait fails the marked test's setup; pytest continues by default while keeping A's
  paper lock (ADR 0069, "Bounds checked 2026-11-11").
* Bad: the guard does not reach code that builds its own HTTP client with the pair.
* Bad: the stream is one connection on many plans, so a sandbox node and a streaming paper test take turns; Alpaca's own
  rules are not simulated.

### Confirmation

* Built 2026-11-11, with term 4's test in `scripts/ci`:
  * `mise run paper:data` reads account A's pair (service `canon-alpaca-paper`) into its own environment, sets
    `CANON_ALPACA_DATA_ONLY=1`, and runs one module from the list `sandbox` (`canon_trading.sandbox_node`) and
    `quotes`, which it refuses as not built yet (ADR 0066); mise refuses any other name before the script
    runs. The sandbox node takes the stream lock after uv's sync, under a bounded deadline
    (`scripts/test-lanes`, under mise, with a stand-in keychain and uv).
  * Term 3: a `get_only` mode of `providers.TimeoutAdapter`, raising `MethodRefused` before anything is
    sent; `with_http_timeout` keeps it once set and replaces an adapter mounted for any other prefix. The
    data client's connect calls `require_get_only` on its historical client before its first request, and
    the provider calls it on its own client each time it fetches it, before anything is sent. `sandbox_node` sets the mode itself, refuses to build outside it, and
    after the build stops if an `AlpacaExecutionClient` exists (read from the collector: the node lists no
    clients at the pin).
  * Term 4 (`packages/canon-alpaca/tests/test_data_only.py`, `tests/test_sandbox_node.py`): submit (POST),
    replace (PATCH), cancel and close (DELETE) through the provider's client, fetched twice, each refused
    with nothing reaching requests' transport, while a GET goes out; a node built with the Alpaca execution
    client refused. Each behaviour test failed with its guard removed (run); the enumeration tests are
    checked instead on made-up source, where an aliased import and an unguarded client are found (run).
  * The build condition: the test enumerates, by name in the source, alpaca-py 0.45.0's `Session()` constructions (one, in
    `RESTClient.__init__`), its `_session` assignments (the same one), its module-level requests calls and
    other HTTP libraries (none), and its `RESTClient` subclasses (eight); and every REST client
    construction in the adapter and `canon_trading` (four: the data client's, the execution client's, the
    provider's and the window's account check's), each passed to `with_http_timeout`. A node in data-only
    mode whose historical or provider client sends on a session the refusing adapter is not on (a
    session replaced before the connect) does not start: the data client's connect fails and the node ends
    with "readiness timeout while waiting for engine connections"; with both clients refusing it starts
    (run, offline).
  * `mise run paper:data sandbox --minutes 3`, 2026-11-11, 07:47:28 to 07:50:34 UTC, exit 0: every engine
    client connected, the account was the sandbox's (`ALPACA-SANDBOX-001`), no request was refused, and the
    tester placed no order, since a built-in streams IEX, which carries nothing overnight (ADR 0055).
    That run was on a working tree before the review's fixes; the run on this change's code is the
    next bullet.
  * `mise run paper:data sandbox --minutes 3` at the branch's ea7c1fa, whose code `main` took unchanged,
    2026-11-11: started 08:09:12 UTC and waited on the stream lock, which a scheduled window held until
    08:15:40; the node started at 08:15:41 and stopped at 08:18:46, exit 0, every engine client connected on
    the sandbox's account (`ALPACA-SANDBOX-001`), no order placed (IEX, overnight), nothing left open, and
    no request refused (run). Fills on this task wait for a run in the regular session (UNVERIFIED until then).
  * A limit of term 3's check: it reads each client's session once, at the connect or the fetch, so a
    session an alpaca-py client replaced after that, which alpaca-py 0.45.0 never does (its one `_session`
    assignment is in `RESTClient.__init__`), would not be checked again.
* A condition on that build, since term 3 holds "fails closed" in one place, the adapter
  `with_http_timeout` mounts: its test enumerates every place the adapter, the provider and alpaca-py
  construct a REST client or a `requests.Session` (a library path that does not pass through
  `with_http_timeout`, a session replaced after the mount), and shows that the node refuses to start in
  data-only mode if any client it holds lacks the refusing adapter.
* `mise run paper:test -- -c pytest.toml -v -s -k sandbox <probe>`, 2026-11-10, 23:18 to 23:20 New York,
  exit 0, 4 passed; the probe is not committed. A `LiveNode` with the Alpaca data client and the sandbox
  client (venue `ALPACA`, `CASH`, `NETTING`, $100k, zero fees). Mid-run the venue's account was the
  sandbox's (`ALPACA-SANDBOX-001`) and no `AlpacaExecutionClient` existed; one `TradingClient` did, the
  provider's by `providers.py` (the probe's own holder check found none and passed on an empty list). A
  small strategy on `boats`: 358 SPY quotes and 9 trades in 96 s; buy filled at 754.26, the ask; sell at
  754.17, the bid; a limit at 90% of the bid accepted and canceled; flat. `EmaCross` (SPY) and
  `GridMarketMaker` (QQQ) as shipped: stream on `v2/iex`, 0 orders in 28 s. Two read-only GETs before and
  after found no open order and no position; they cannot show nothing reached Alpaca, which rests on there
  being no Alpaca execution client. This run started at 23:18:28 and its node at 23:18:30; an earlier run started
  at 23:00:51 and its node at 23:16:11, a wait for the lock inferred, not logged. Three earlier runs failed in the probe's code: reading node state mid-run and
  a cancel by order object; `Cache.accounts`; a check that counted the provider's `TradingClient`.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed here.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/examples/live/sandbox/exec_tester.py
  — the sandbox beside a venue's live data client. Fetched. `…/crates/adapters/sandbox/src/config.rs` and
  `src/execution.rs`, `…/docs/concepts/positions.md` at the commit — as cited. Fetched.
* NautilusTrader v2.0.0rc9 release notes (published 2026-11-10) — the sandbox fixes. Fetched by ADR 0068's
  second reader; the pin is rc8 (`scripts/canon outdated`).
* nautilus-trader 2.0.0rc8 in `.venv` — `SandboxExecutionClientConfig`, `add_simulated_exec_client`,
  `order_id_tag` refusal. Run.
* `canon_alpaca` `data.py`, `providers.py` — one stream per client, the IEX default, the provider's
  `TradingClient`. Read.
* https://docs.alpaca.markets/us/docs/streaming-market-data.md — the connection limit, 406. Fetched.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md — 30 symbols on Basic. Fetched.
* https://docs.alpaca.markets/us/docs/paper-trading.md — what paper simulates. Fetched.
* https://docs.alpaca.markets/us/reference/get-v2-assets-1.md — the trading hosts only. Fetched by a scout.
* https://docs.alpaca.markets/us/docs/credential-management.md (Broker API) and
  https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api.md (scopes). Fetched by a scout and
  ADR 0068's second reader.
* alpaca-py 0.45.0 in `.venv` — the four calls' methods (POST, PATCH, DELETE, DELETE), all through
  `RESTClient._session`. Read by the second reader.
* Second reader (`evidence-verifier`), 2026-11-10: a refusing adapter separate from `TimeoutAdapter`
  would be replaced by `with_http_timeout` (an offline run); the wash-trade refusal was cited to the wrong
  page; the run's start times were another run's; pair A and ADR 0069 were undefined; "one stream per
  login" overstated; ADR 0066 is `proposed`; a third module needs a new record. All applied above.
* Follow-up, 2026-11-11: the build's enumeration condition added to Confirmation; the owner's question
  dated.
* alpaca-py 0.45.0 in `.venv` — `RESTClient._one_request` sends through `self._session.request`, and an
  error other than `HTTPError` leaves its retry loop, so `MethodRefused` ends the call. Read and run,
  2026-11-11.
* NautilusTrader v2.0.0rc8 in `.venv` — `LiveNode` exposes no list of its clients; `nautilus_trader.adapters.sandbox`
  is the example's import path at the pin; a data client whose connect fails leaves the hosted run ending
  with "readiness timeout while waiting for engine connections". Run, 2026-11-11.
* Correction, 2026-11-11: the consequence now names the account whose stream is held and the waiter
  that fails. Read at `7d28f77`: `mise.toml:393,458,469-479,506-535` (B's separate stream lock, A's
  streaming modules, the 900 s wait and data deadlines), `src/canon_trading/sandbox_node.py:86-88`
  and `src/canon_trading/movers_stream.py:88` (60-minute runs), and `tests/conftest.py:323-365`
  (failure of one marked test's setup). ADR 0069 records the remaining waiters and pytest's default
  continuation. The limits are checked in source; actual paper durations remain UNVERIFIED here.
