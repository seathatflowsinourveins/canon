---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2027-02-08
---

# Two Alpaca paper accounts: A for the paper tests, B for the session windows

## Context and Problem Statement

One paper account serves `paper:test` and `paper:session` under one lock (ADR 0029, ADR 0044). A test that
leaves an order or a share there in the window's symbol makes the next window refuse to start, and a test can
lose the lock to the next window. ADR 0044 named the remedy, a second paper account for session runs, as the
owner's to create, and the owner has created it. How is the second pair kept, and what keeps each account to
its use?

## Decision Drivers

* Each pair keeps every property ADR 0029 and ADR 0033 give the first: it is in no file, command line or
  output, and it is checked against the paper API before it is stored.
* A pair reaches one account: read from Alpaca's new keys "for any newly created account"; no page read
  states it (UNVERIFIED). So the tasks keep each account to its use by which pair each one reads, not by
  convention.
* Every existing use is unchanged.

## Considered Options

1. A copy of each of the three keychain tasks per account (`paper-windows:store` and so on).
2. The three tasks take the account as an argument, from a fixed list, with account A as the default.
3. As option 2, and `paper:store` also compares the account `GET /v2/account` names for the pair entered
   with the other account's (its `id` and `account_number`).

## Decision Outcome

Chosen: **option 2**. Each task has one body, so the two accounts cannot drift apart between copies.
Option 3 would also catch two pairs of one account, but it needs the other account's id kept somewhere or
its secret read to ask the API; it is left for when the reading side is built.

| Account | Name | Keychain service | For | Never for |
|---|---|---|---|---|
| A | `tests`, the default | `canon-alpaca-paper` | `paper:test` | a session window |
| B | `windows` | `canon-alpaca-paper-windows` | `paper:session` and its schedule (ADR 0050) | a paper test |

Each service holds ADR 0029's two items, accounts `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. The variable
names, the adapter and `canon_alpaca` stay as they are. B's items are labelled `Alpaca-paper-windows-<item>`.

1. **`mise run paper:store|paper:check|paper:remove [tests|windows]`.** Each task's mise `usage` spec takes
   one optional argument, default `tests`, with the choices `tests` and `windows`. mise refuses any other
   name, or a second word, before the script starts, and each script refuses a name outside the two again.
   `paper:store windows` keeps all of ADR 0033: it needs a terminal, turns echo off before its hidden
   prompts, refuses an `AK` key ID, stores only on a 200 from the paper API, passes values on stdin only,
   reads both back, and removes both on any failure.
2. **No pair in both accounts.** `paper:store` refuses a key ID equal to the one stored for the other
   account, before it asks the paper API. Two equal pairs have equal key IDs, so this catches the same pair
   entered for both accounts without reading the other account's secret. It also refuses a pair that
   shares only the key ID, which is not the same pair; whether Alpaca ever issues one is UNVERIFIED.
   `security find-generic-password -w` writes the stored key ID to a pipe into the task's shell, and bash's
   `[`, a builtin, compares the two, so neither is in any process's arguments, printed or written, as in
   ADR 0033's read-back. The read comes before the prompts, bounded at 30 s; any status but 44 (absent)
   stops the task. The refusal holds both ways, whichever pair was entered first.
3. **Locks, decided now, built when a task reads account B.** Each account gets its own lock:
   `canon-paper-test.lock` for A, as today, and `canon-paper-windows.lock` for B, so a test and a window
   no longer wait for each other. Each account's pair gets its own lock for the market-data stream,
   held by any run that opens a stream on that pair and taken after the account's lock:
   `canon-alpaca-stream-tests.lock` for A's pair (a paper test that streams, and `paper:data`'s streaming
   modules) and `canon-alpaca-stream-windows.lock` for B's (a window). Until 2026-11-11 one lock,
   `canon-alpaca-stream.lock`, served the whole login; it was split on these observations, each one
   observation, on paper that day: (1) 12:11:01 to 12:12:01 New York, a `sip` connection on B's pair,
   subscribed to SPY trades, authenticated and received 930 trades with no 406, no error and no drop,
   while a `sip` connection on A's pair carried 223,840 messages in the same minute, none of its seconds
   under 2,508; (2) 12:41:10 to 12:42:10, a second `sip` connection on A's pair beside a whole-market
   recorder on the same pair authenticated, received 1,386 SPY trades, the first after 0.32 s, with no
   406 and no drop, and the recorder's minute was normal (3,823 bars, 218,338 trades); (3) 12:42:32 to
   12:43:33, an `iex` connection on A's pair beside the same recorder authenticated and received 231 SPY
   trades, the first after 0.51 s, with no 406 and no drop, the recorder's minutes normal (3,927 and 3,823
   bars). At most two connections per pair are used: the holder of that pair's stream lock, and a recorder
   that takes no lock, never opens a third connection, and reports a 406 by name without retrying around
   it. No task here is such a recorder: a recorder runs outside the tasks and keeps to these terms
   itself; nothing here enforces them. B's stream lock is taken only by a window, which already holds B's lock, so today it
   never waits; it is there for the next run that streams on B's pair. Two lock holders on one pair stay excluded; that case is not measured. The trading API's request limit is per
   account, "200 requests per minute, per account" (a support page that does not name paper accounts, so
   for paper UNVERIFIED). The stream limit belongs to the user: "in many subscriptions (or without one)
   this limit is 1". Read from "from a user", two paper accounts of one login would share one stream, but
   no page states it for a second paper account, and the observations above found otherwise (one pair also
   held two connections at once on 2026-11-11, Confirmation, "The open ends"). Alpaca support states that paper
   accounts are not eligible for the increased API rate (private correspondence, read 2026-11-11; not
   publicly documented), so paper work is sized for the standard 200 a minute. Alpaca staff forum posts say the
   200 is "per account (ie each paper and live account is separate)"
   (https://forum.alpaca.markets/t/what-constitutes-an-api-call/16502, 2025-03-24); that is a forum
   statement, not documentation, so the per-account question stays UNVERIFIED.
4. **What this change builds:** storing, checking and removing B's pair, and an optional
   `paper key pair (windows)` row in `scripts/bootstrap --check` that shows present or absent and never a
   value. **What it does not build yet:** no task puts account B's pair in an environment; the only read
   of it is `paper:store`'s comparison of B's key ID when A's pair is stored. `paper:test` and
   `paper:session` both still read A under one lock. A later change moves `paper:session` and its
   schedule to B and adds the locks in clause 3; until then account B sits unused. (Built 2026-11-11:
   Confirmation, "The reading side".)

This amends ADR 0029, whose tasks act on one service, and, once a window reads B, ADR 0044's "Sharing the
account" and ADR 0050's schedule. Each of the three points here.

### Consequences

* Good: with no argument, the three tasks act on account A as before. `paper:check` and `paper:remove`
  print one more line, naming the account and service, and `paper:store` makes one more read, B's key
  ID, which can refuse.
* Good: once `paper:session` reads B, a failed test cannot stop a window, and each account has its own
  request limit (UNVERIFIED for paper, clause 3).
* Bad: the refusal catches the same pair entered for both accounts. It does not catch two different pairs
  of one account: neither the keychain nor the paper API check here says which account a pair reaches.
* Bad: two runs that each open a stream on one pair still wait for each other on that pair's stream lock;
  runs on the two pairs do not, on the observations of 2026-11-11 in clause 3, which are single
  observations, not a documented limit.

### Confirmation

`scripts/test-lanes`, each task's body from `mise.toml` with a `security` stand-in: `windows` uses B's
service only, no argument or `tests` A's only, any other name is refused before `security` is called.
`paper:store windows` refuses A's key ID before the paper API, reads A's key ID once and A's secret never,
stops before any prompt when A's key ID cannot be read, and otherwise stores B's two items under B's label
with no value in a `security` argument; A's store refuses B's key ID. The three tasks, copied by name, run
under the real mise, which picks the service from the argument and refuses `live` with no `security` call.
On a pseudo-terminal, with the real `security` and throwaway services, `paper:store windows` refuses A's
pair and stores another, which reads back equal under B's label, A's items unchanged. Against the tasks as
they were, 20 cases fail (run): every case for `windows`, a refused name and the same-pair refusal.

The trial, 2026-11-10, mise 2026.11.5, a throwaway folder: the `usage` spec above gave `tests` with no
argument and `windows` with one. `bogus` failed with "Invalid choice", a second word with "unexpected word",
an inherited `usage_account` was cleared, and no argument was appended to the script. `type [` in bash
3.2.57 (`/bin/bash`, the bash the tasks get): "[ is a shell builtin". `security` showed an item's label as
attribute `0x00000007` (macOS 27.2).

The owner's step: `mise run paper:store windows`, then `mise run paper:check windows`.

Account B's pair was stored on 2026-11-11 and checked again the same day: `mise run paper:check windows`
exit 0, both items in the login keychain; `mise run paper:check` exit 0, account A's items intact;
`scripts/bootstrap --check` shows both rows ok, `paper key pair` and `paper key pair (windows)` (run).

**The reading side, built 2026-11-11.**

* `paper:session`, by hand and from ADR 0050's schedule, reads only account B's pair (service
  `canon-alpaca-paper-windows`); `paper:test` reads only account A's. Neither reads the other's.
* The locks of clause 3. `paper:test` holds `canon-paper-test.lock` for its run. `paper:session` holds
  `canon-paper-windows.lock`, then, after uv's sync, `canon-alpaca-stream.lock`, on descriptor 7, since the
  runner's checkout lock is 8. Both waits come out of its one 900 s wait, so ADR 0050's deadline sum is
  unchanged. Each wait is a non-blocking `lockf -t 0 <fd>` and a one-second sleep (`lock_wait` in mise.toml), not `lockf -t <secs> <fd>`: measured here on macOS, the descriptor form busy-loops, 29.3 s of CPU over a 30 s wait against 0.03 s for the poll. A paper test takes the stream lock per test, not per run: a lock for the whole run would make
  every test wait for every window, since both stream. A paper test module that builds a data client or a
  stream carries the `alpaca_stream` marker. Its fixture, one block in both conftests, takes the stream lock
  with `fcntl.flock` for that test, at the path `paper:test` names (`CANON_ALPACA_STREAM_LOCK`). The wait
  blocks in flock(2), as lockf(1)'s does, under an interval timer, at most 3900 s and never past the point
  where 900 s would be left before `paper:test`'s deadline, which it also exports. The lock is let go only
  once every stream the test started has returned from `run()`, waited for up to 60 s; a stream still
  running then fails the test, and the lock is kept until the run ends. A paper test without the marker
  that starts an alpaca-py market-data stream is stopped in `DataStream.run`, before it connects, and fails. The stream lock is always taken last, after
  an account's lock or alone, so no run waits for an account's lock while it holds the stream. What each
  run waits for is ADR 0050's arithmetic.
* The no-ask rule for paper runs covers both accounts, each under its own lock. The owner decided on
  2026-11-11 that paper end-to-end runs on either account, at any hour, started by a lane, a coordinator or
  the scheduled agent, need no ask; live trading is untouched (`.claude/rules/trading-safety.md`).
* `scripts/test-lanes`: each task's service and lock read from `mise.toml`. Under the real mise, with one
  lock folder, a stand-in window holds B's lock and the stream lock, and a stand-in test on A meanwhile
  runs at once and is told the stream lock's path. A window whose stream lock is held waits its
  (shortened) wait and fails, with no window started and B's lock free. Against the tasks as they were,
  8 cases fail (run). `tests/test_session_window.py`: the two conftests' blocks are equal, and their wait
  is paper:test's and `PAPER_LOCK_WAIT_SECS`. Every paper module that streams is marked. The fixture's
  lock gives up at its bound while lockf(1) holds the file, and lockf cannot take it while the fixture
  holds it. An unmarked stream is stopped in `run()`. The wait stops before the deadline's room, and a
  stream is tracked until its `run()` returns (run).
* On paper, 2026-11-11, at `main`'s 93f23c0: `mise run paper:session --session auto --minutes 10` on account
  B, 07:34:32 to 07:44:40 UTC, result `ok`, 2 fills, nothing left open; inside it, from 07:35:32 to 07:35:43,
  `mise run paper:test -- -v -rs -k "flat_reconciliation or quotes_write_back"` on account A, exit 0, 4
  passed, neither waiting for the other (run); the test ran from a checkout at 93f23c0 whose working tree
  was not compared with it (UNVERIFIED). The account state the window logged differed from account A's
  (compared in the two logs; the values are not recorded). The
  first scheduled firing on B, 2026-11-11 at `main`'s e7b5d5c: the runner started at 07:48:37 UTC and the
  window ran from 07:50:35 to 08:15:40, result `ok`, 2 fills, nothing left open, so the task's keychain
  reads of B's items, each bounded to 30 s, finished with no dialog waiting (the runner's log and the
  window's record, read).
* Each window record names its account by name, never by key: `paper:session` sets `CANON_PAPER_ACCOUNT=windows`
  beside its reads of B's pair; `session_window` writes `account`, `windows` or `tests` and any other value
  as `unknown`, and the
  paper session row of `scripts/automation status` shows it, `not recorded` for an older record
  (`tests/test_session_window.py`, `scripts/test-lanes`; run). On paper, 2026-11-11, at f4a0016, this
  change's code before it was rebased onto a `main` that had changed none of its files: `mise run paper:session --session auto --minutes 5` on
  account B, 09:13:37 to 09:18:45 UTC, recorded `account: windows`; nothing placed and nothing left open.
  The window itself failed, since IEX delivered no SPY quote or trade in 307 s of the early pre-market
  (run).
* Option 3's comparison of account IDs is still not built.

**One stream lock per pair, built 2026-11-11** (clause 3). `paper:test` names
`canon-alpaca-stream-tests.lock` to its fixture (`CANON_ALPACA_STREAM_LOCK`); `paper:data`'s streaming
modules take the same lock; `paper:session` takes `canon-alpaca-stream-windows.lock` after B's lock. The
two tasks' stream waits now try lockf(1) `-t 0` once a second and sleep in between, as ADR 0065's
`waitlock` does, since lockf's own `-t` on a descriptor spins a core while it waits; a lockf error other
than 75 ends the wait at once. The maintenance agent's round now checks A's stream lock whether or not a
window runs, and no longer the old file. `scripts/test-lanes`, under the real mise with one lock folder: a
window on B starts while A's stream lock is held as the fixture holds it; a window waits for B's stream
lock and fails past its wait; `paper:data` waits for A's stream lock and runs while B's is held; no task
names `canon-alpaca-stream.lock`. `scripts/test-maintenance`: a round does nothing while A's stream lock is
held, with or without a window. Against the tasks as they were, 11 and 2 of these cases fail (run). At
the change, a window or a `paper:data` run started before it holds the old `canon-alpaca-stream.lock`
until it ends, and the maintenance round does not see it; nothing built after the change takes or waits
for that file (`scripts/test-lanes`, run), so a test that streams
on A no longer waits for that window. Whether A's pair and B's pair share one count of connections, left
open below, is answered for `sip` by observation (1) in clause 3. The window's longest run,
`session_window.LONGEST_WINDOW_SECS`, is still sized for a streaming test waiting behind a window for
one shared lock; it is left as it was. A streaming test's 3,900 s wait for A's stream lock can run out
behind a `paper:data` stream of 60 minutes run to its deadline; it is not changed here.

**The open ends, checked 2026-11-11.** Each clause marked UNVERIFIED above, and what came of it:

* The stream limit (clause 3). On paper, 2026-11-11, from 15:21:05 to 15:21:49 UTC, at 349fc9e with a
  working tree that differed only in two records, under `mise run paper:test` (account A's lock), with B's
  lock and the stream lock taken without waiting, so no window streamed: a probe kept outside the
  repository opened two connections to the IEX stream with account A's pair, sending the frames
  alpaca-py's `_connect` and `_auth` exchange. Both answered `connected`, then `authenticated` (15:21:44 and 15:21:46 UTC).
  The first was still open after the second authenticated. Nothing was subscribed or placed, and the run
  exited 0 (run). So on this login one pair held two authenticated connections to one endpoint for about
  2 s, with nothing subscribed. Whether a second connection that subscribes is refused, as the page's "only
  1 active stream connection" would have it, is UNVERIFIED. The stream lock is kept as decided. Whether A's pair and B's pair share one count of connections is still UNVERIFIED: a probe needs
  B's pair, and only `paper:session`, which runs the window and nothing else, puts B's pair in a process.
* The request limit (clause 3) stays UNVERIFIED for paper. Alpaca's paper-trading skill says every
  response carries `X-RateLimit-*` headers (ADR 0044). alpaca-py 0.45.0's `RESTClient._one_request`
  returns only the parsed body, so no documented alpaca-py path gives the headers. Settling it needs a
  read of one response's headers on each account in the same minute, by two runs, one under each task,
  each with a request outside alpaca-py.
* A pair reaching one account (Decision Drivers) and a key ID shared by two pairs (clause 2) stay
  UNVERIFIED. No Alpaca page read states either. Option 3's comparison of the `id` that `GET /v2/account`
  returns would settle the first for the two pairs stored here.
* The test beside the first window, whose working tree was not compared, stays UNVERIFIED for that run. It
  was run again on paper, 2026-11-11, from 15:22:47 to 15:23:37 UTC, at 349fc9e with a working tree that
  differed only in two records (`git status`):
  `mise run paper:test -- -v -rs -k "flat_reconciliation or quotes_write_back"` on account A, exit 0,
  4 passed. A window that `mise run paper:session --session auto --minutes 5` had started at 15:22:14
  held B's lock throughout, and the stream lock from about 15:23:09, when its node started. The test did
  not wait for B's lock. These tests open no stream, so the stream lock was not exercised. The window
  exited 0 at 15:29:15 UTC: result `ok`, 2 fills, nothing left open, record `account: windows` (run).
* The scheduled series on B, 2026-11-11, from the first firing at 07:48:37 UTC to the one at 15:08:42 UTC
  (the runner's log and the window records, read). There were 15 firings. Three ran a window: 07:50 was
  `ok` with 2 fills, and 09:25 and 10:25, in the pre-market, failed with no IEX quote or trade and placed
  nothing. Each of the three ended with nothing left open. Of the other twelve, seven were pre-market
  firings skipped by name (ADR 0044), and one skipped with under 635 s left in the overnight session.
  Three skipped because a window started by hand on B had ended under 300 s before. One gave up after
  900 s on B's lock, which a window started by hand held. The record of B's stored pair is above
  ("Account B's pair was stored").

## Evidence

Read 2026-11-10.

* https://docs.alpaca.markets/us/docs/paper-trading.md — "create and delete paper accounts, rather than
  resetting them"; "Open New Paper Account"; "generate new API keys for any newly created account"
  (fetched).
* https://alpaca.markets/support/usage-limit-api-calls — "200 requests per minute, per account" (fetched).
* https://alpaca.markets/elite — lists API call limits of "200 calls / minute" and "1000 calls / minute"
  by routing tier: 200 under commission-free, 1000 under the Elite Smart Router (fetched, summarised, not
  raw). No public page found
  says whether paper accounts get the higher rate; that rests on Alpaca support, private correspondence,
  read 2026-11-11, and is not publicly documented.
* https://docs.alpaca.markets/us/docs/streaming-market-data.md — "The number of connections to a single
  endpoint from a user is limited based on the user's subscription, but in many subscriptions (or without
  one) this limit is 1"; "only 1 active stream connection"; `connection limit exceeded` (fetched).
* https://raw.githubusercontent.com/jdx/mise/v2026.11.5/docs/tasks/task-arguments.md — the `usage` field,
  `choices`, "A missing required argument or a value outside the choices fails before the script starts",
  `usage_<name>` variables, "mise clears `usage_*` variables inherited from the calling environment",
  "Without a spec, mise forwards extra arguments" (fetched at the tag `scripts/bootstrap` pins, the mise
  that ran the trial; the trial, run).
* `man security` and ADR 0033 — `find-generic-password` `-w` displays only the password; `-i` and `-X`
  as ADR 0033 uses them (read).
* https://docs.alpaca.markets/us/reference/getaccount-1.md — `GET /v2/account` returns the account's `id`
  and `account_number` (fetched by the second reader; option 3).
* ADR 0029, ADR 0033, ADR 0044 and ADR 0050, for the tasks, the lock and the schedule this builds on.
* `man lockf` on this machine: "BSD-style locking is used, as described in flock(2)" (read). A Python
  `fcntl.flock` on the file and lockf(1) exclude each other both ways, lockf exiting 75 while Python holds
  it (run, 2026-11-11).
* alpaca-py 0.45.0 in `.venv`, `alpaca/data/live/`: `StockDataStream`, `CryptoDataStream`,
  `OptionDataStream` and `NewsDataStream` subclass `DataStream`, and only `DataStream` defines `run` (read).
* Read 2026-11-11: https://docs.alpaca.markets/us/docs/streaming-market-data.md, the same lines and the
  406 `connection limit exceeded` row (fetched again).
  https://github.com/alpacahq/alpaca-py/blob/c42add7c0a7026e2ead3ed64e76f4def2b99fda4/alpaca/common/rest.py
  lines 179 to 213: `_one_request` returns `response.json()`, and the headers are not returned.
  `alpaca/data/live/websocket.py`, lines 169 to 222: `_connect` waits for `connected`, and `_auth` sends
  `auth` and expects `authenticated`. Both were read in the local copy at the pin. The paper-trading skill's
  `X-RateLimit-*` line is cited in ADR 0044's Evidence.
* The 2026-11-11 runs: their saved logs and the window records are kept outside the repository (run).
* Reviews, 2026-11-10. `/code-review` (high): nine findings, seven fixed (the real-mise cases' environment
  and copy; the other key ID read before the prompts, bounded; the help; `paper:check`'s exit 3 for a bad
  name; the key-ID claim), two left: the README's paper section, for the change that moves
  `paper:session` to B, and one map per body, each checked by a `windows` case. Second reader
  (`evidence-verifier`): it corrected "no task reads account B", "as before", the request limit, a pair
  reaching one account, the window's symbol and the mise source line, and found option 3. All fixed above.
