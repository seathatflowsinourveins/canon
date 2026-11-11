---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2027-02-08
---

# Alpaca HTTP timeout: a requests transport adapter on alpaca-py's private session

## Context and Problem Statement

alpaca-py 0.45.0 sends every request through `RESTClient._request` with
`self._session.request(method, url, **opts)` and no `timeout`, and its clients take no timeout setting.
requests: "By default, requests do not time out unless a timeout value is set explicitly. Without a
timeout, your code may hang for minutes or more." On paper, on 2026-11-10, a node stopped right after
start-up logged "Failed to disconnect execution clients: ALPACA" and its process never ended, holding the
shared paper lock for about 47 minutes until it was killed; a request that never answered fits that, but
is not proven. How does the adapter bound its HTTP requests, when alpaca-py offers no way?

## Considered Options

1. A requests transport adapter, mounted on each alpaca-py REST client's `requests.Session`, that supplies
   a default (connect, read) timeout to a request that passes none.
2. Bound only the adapter's wait for each call, leaving the request running.
3. Wait for alpaca-py to offer a timeout.

## Decision Outcome

Chosen: **option 1.** It ends the request itself, for every alpaca-py REST client the adapter builds (the
instrument provider's and the execution client's `TradingClient`, the data client's
`StockHistoricalDataClient`, built by the adapter or passed in), so nothing is left running and nothing
holds the client's one-call-at-a-time lock.

**The departure from rule 3.** The mechanism is requests' own: "Requests enables users to create and use
their own Transport Adapters ... Once created, a Transport Adapter can be mounted to a Session object"
("Transport Adapters"). Only the way to the session is private: alpaca-py keeps it in
`RESTClient._session`, an underscore attribute no alpaca-py document mentions. The adapter's code names
the attribute (`providers.with_http_timeout`), refuses at start-up a client that holds no
`requests.Session` there, and `test_upstream_contract.py` checks at the pin that `TradingClient` and
`StockHistoricalDataClient` hold one and that alpaca-py still passes no timeout. A public timeout option in
alpaca-py replaces this: issue 415, "Support REST requests timeout, especially with
StockHistoricalDataClient", open since 2024-03-06, assigned, with no comment; no release after 0.45.0
and no open pull request adds one.

**The values: connect 3.05 s, read 27 s.** Both are requests' own: "It's a good practice to set connect
timeouts to slightly larger than a multiple of 3, which is the default TCP packet retransmission window",
and its example `timeout=(3.05, 27)`. Upstream's adapters at the pinned commit set HTTP timeouts in the
tens of seconds (Kraken `timeout_secs` 30, OKX `http_timeout_secs` 60). The read timeout bounds each wait
"between bytes", not the whole response, and a connect timeout applies per address tried.

**With alpaca-py's retry and the node's disconnect.** alpaca-py repeats a request answered 429 or 504 up to
3 times, 3 s apart, but not an order command (below); a timeout is not an HTTP status, so it is not repeated.
One attempt that hangs now ends within about 30 s; a read repeated three times after 429s can take about
four attempts' time plus 9 s. The
paper node's disconnect timeout is 15 s, shorter than the read timeout, so a call hanging when the node
stops can still outlast the disconnect; that the run then ends once the read timeout fires, rather than
never, follows from the request ending, and is UNVERIFIED on a stopping node.

**Order commands are sent once.** alpaca-py's repeat covers a submit, a replace and a cancel too, and
`TradingClient` takes no retry setting (the `RESTClient` arguments it does not pass cannot turn the repeat
off either: a zero or empty value keeps the default). NautilusTrader's policies send a submit, modify or
cancel "Once, unless the safety test holds", which only "stable request identity and duplicate detection,
or ... idempotent semantics" can meet, and keep a read's repeat; in its conformance table Architect AX,
Binance, Coinbase, Derive, Hyperliquid and Lighter send order commands once, and Betfair, the one that
repeats them, reuses a stable `customerRef`. A repeat here would fail the test's third condition, that it
"Cannot report an earlier success as a rejection". Alpaca's FAQ says not to resend an order that
timed out, and on paper it refuses a repeated `client_order_id` with 422 (Confirmation), so a repeat would
turn a taken submit into a refusal. So the execution client adds a requests response hook ("Event Hooks")
to the same private session at connect (`execution.send_order_commands_once`, refused without a session as
the timeout is): while an order command runs on its worker thread (`without_resend`), an error answer
raises alpaca-py's own `APIError`, built as `RESTClient._one_request` builds it, before alpaca-py can repeat
the request. The adapter does not set alpaca-py's private retry state (`_retry`, `_retry_codes`): that
would stop reads' repeats too, and the hook reaches nothing the timeout does not already reach. (The
session window's own client, outside the adapter, sets `_retry = 0` for its bounded account check, ADR
0044.) A 504 or 429 is then an unknown outcome, "An HTTP 5xx without definitive command evidence is
ambiguous" and "rate limits are definitive only when venue-specific semantics prove that the command was
not accepted", which Alpaca's pages read do not do for the Trading API (its one rate-limit page is the
Broker API's), and the order is left for the stream, the engine's in-flight `QueryOrder` or
reconciliation. A public retry option in alpaca-py
(open pull request 870 lets a zero retry count disable the repeat on `RESTClient`, not `TradingClient`)
would replace the hook.

**Outcomes.** requests' `ConnectTimeout` and `ReadTimeout` are `OSError`s, which the execution client
already treats as transport errors. requests' connect timeout is the wait "to establish a connection",
before the request is sent, so one on a call's only attempt proves the request was not sent: a submit is
rejected, a modify modify-rejected and a cancel cancel-rejected, each with the reason "not sent", as
NautilusTrader's policies give for a command "proven unsuccessful". The transport adapter counts each
call's sends on the thread that makes them and the count travels on the exception, because a connect
timeout on alpaca-py's repeat of a 504 does not prove the first attempt did not arrive. Any other timeout,
a read timeout above all, leaves a submit `SUBMITTED` and a cancel as it was, the client's existing
handling of an unknown outcome; policies: "Transport errors, timeouts ... usually leave the venue outcome
unknown." A stall in the TLS handshake sends nothing either, but urllib3 raises it as a read timeout, so
it stays unknown: the safe direction.

### Consequences

* Good: a request that never answers ends at the read timeout; the next call works; the node's run ends.
* Good: the data client's historical requests and the provider's asset requests are bounded the same way.
* Bad: a valid request whose first byte takes longer than 27 s, a large bar query perhaps, now fails, and
  alpaca-py does not repeat it; how long Alpaca can take is UNVERIFIED.
* Bad: the adapter depends on an alpaca-py private attribute until alpaca-py offers a timeout; a pin move
  can break it, and the contract test then fails.
* Bad: the stream (alpaca-py's websockets) is outside this; its stop stays bounded by the client's join.

### Confirmation

`scripts/canon lint` checks this record. `test_upstream_contract.py` fails if alpaca-py's REST clients stop
holding a `requests.Session` at `_session`, or `_request` or `_one_request` start to name a timeout (then
move to alpaca-py's).
`test_execution_client.py` runs alpaca-py's `RESTClient` against a local server that never answers: the
call ends at the read timeout, the next is answered, and the node's run ends; without the adapter that test
hangs. It also covers each outcome above. `with_http_timeout` mounts the package's adapter on both `https://` and
`http://` unless that one adapter, at (3.05, 27), is already on both, so a client passed in with a
`TimeoutAdapter` at another timeout, or on HTTPS only, gets the package's; the same test file covers it.
The paper hang, read again from its log on 2026-11-10: the node's disconnect failed 144 s after it began,
its 15 s wait for the engines ended about 7.5 minutes later, and the process lived on after the node was
disposed, while the test's own alpaca-py client, with no timeout, did its clean-up. Not reproduced
offline: a trade stream that ignores its stop gives the same two log lines within the configured bounds.
Two waits in the adapter had no bound of their own and now have one, each with an offline test that
fails without it: the disconnect waited for a stream thread through a worker of the loop's default
executor, where every alpaca-py call also runs, and for the cancelled trade-update task with no limit.
Each paper test's own alpaca-py client now has this adapter's timeout. What delayed the node's
disconnect on paper stays UNVERIFIED. On paper, the execution rows of `test_paper_matrix.py` run with
the adapter at the next regular session (UNVERIFIED until then). Before moving the alpaca-py pin, read its
notes for a timeout option and for `RESTClient`.

**Order commands sent once.** `test_execution_client.py` runs alpaca-py's own `TradingClient` and
`RESTClient`, its retry included, over a stand-in for the network under requests' `HTTPAdapter` that
answers every request 504: unchanged, alpaca-py sends a cancel four times (the upstream behaviour, a test
that fails once alpaca-py stops repeating, when the hook can go); through the execution client a submit, a
replace and a cancel are each sent once and left unknown, and a read is still sent four times. The three
order-command tests failed before the hook, each with four requests sent (run, 2026-11-10). The hook
covers error answers only, and needs nothing more, by two facts each with a test: alpaca-py repeats only
on its own `RetryException`, raised for a status in its retry codes, so a transport error, a timeout
above all, propagates unrepeated (`RESTClient._request`); and the transport retries nothing, since
`TimeoutAdapter` keeps requests' `HTTPAdapter` default, `Retry(0, read=False)`. Over the same stand-in, a
submit, a replace and a cancel meeting a connect timeout or a read timeout are each sent once, the
connect timeout's rejected "not sent" and the read timeout's left unknown; the adapter mounted on the
trading client's session is asserted to retry nothing; and a POST to a local server that never answers,
through the real requests and urllib3 stack, reaches it once. Paper, 2026-11-10, overnight,
`mise run paper:test` (`test_paper_resent_client_order_id_is_refused_and_one_order_rests`): a submit
repeating a resting $1.00 buy's `client_order_id` was answered 422
`{"code":42210000,"message":"client_order_id must be unique"}`, and Alpaca held one open order under that
ID, the first. Not shown by any run: that Alpaca answers 504, or anything, when an order request times out
on its side; Alpaca's pages read do not say what the FAQ's "timeout message" is (UNVERIFIED).

## Evidence

Read 2026-11-10.

* alpaca-py 0.45.0 as installed: `alpaca/common/rest.py` (`Session()` at `self._session`;
  `_one_request` calls `self._session.request(method, url, **opts)`; `opts` has no `timeout`; retry on
  `RetryException` only) and `alpaca/common/constants.py` (`DEFAULT_RETRY_ATTEMPTS = 3`,
  `DEFAULT_RETRY_WAIT_SECONDS = 3`, `DEFAULT_RETRY_EXCEPTION_CODES = [429, 504]`) (read).
* https://raw.githubusercontent.com/psf/requests/v2.34.2/docs/user/advanced.rst — "Timeouts" and
  "Transport Adapters", quoted above (fetched, raw).
* https://raw.githubusercontent.com/psf/requests/v2.34.2/docs/user/quickstart.rst — "Nearly all production
  code should use this parameter in nearly all requests" (fetched, raw).
* requests 2.34.2 as installed: `ConnectTimeout` and `ReadTimeout` subclass `OSError` (run).
* https://github.com/alpacahq/alpaca-py/issues/415 through the GitHub API — open, assigned, no comments;
  a title search for "timeout" found besides it #628, #648 and #864 (stream, closed) and #211 (a REST
  timeout from Alpaca's side in 2022, closed) (fetched).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/adapters/kraken/src/config.rs
  and `crates/adapters/okx/src/config.rs` at the same commit — the default timeouts above (fetched, raw).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/policies.md
  — "Command outcomes", the event for a submit or cancel proven unsuccessful, and unknown outcomes
  (fetched, raw).
* `canon/upstreams.json` entry `requests`, resolved to `v2.34.2` @ `6e83187b8feb`: requests was installed
  as alpaca-py's dependency and is now imported directly by the adapter, declared in
  `packages/canon-alpaca/pyproject.toml` at the version `uv.lock` already held (resolved).
* The paper hang: a run of the fill-report paper test (not yet landed), 2026-11-10, its log showing "Error
  disconnecting clients: Failed to disconnect execution clients: ALPACA" two and a half minutes after the
  stop, then "Timed out (15s) waiting for engines to disconnect"; the process's elapsed time when killed,
  47 minutes (`ps`); its cause is consistent with a request that never answered, not proven (run).
* The paper run's log, read again 2026-11-10: "Disconnecting clients..." at 19:55:11.5 UTC, "Error
  disconnecting clients: Failed to disconnect execution clients: ALPACA" at 19:57:36.0, "Timed out (15s)
  waiting for engines to disconnect" at 20:05:12.9, then the node disposed and no further line until the
  task was killed (exit 143) (read). Offline, a trade stream that ignores `stop` with a 2 s join gives the
  same two errors at 2 s and 5 s (run).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/crates/live/src/task.rs
  (`TaskGroup::finish_shutdown`, "Drains the closed generation within graceful and forced bounds") and
  `crates/adapters/coinbase/src/execution.rs` at the same commit (`finish_shutdown(Duration::from_secs(1),
  Duration::from_secs(2))` in its disconnect) (fetched, raw).
* alpaca-py 0.45.0 as installed, `trading/stream.py`: `stop()` "never blocks"; `_auth` awaits
  `self._ws.recv()` with no timeout, under websockets' `ping_interval` 10 and `ping_timeout` 180 (read).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/policies.md
  ("Delivery and retry limits": "An adapter retries a transient failure only when a repeat is safe"; the
  table's submit, modify and cancel rows, "Once, unless the safety test holds"; a read, "On a transient
  failure"; "Command outcomes": "HTTP status codes and rate limits are definitive only when venue-specific
  semantics prove that the command was not accepted") and `docs/developer_guide/adapters.md` at the same commit ("Send submit ... modify, cancel ...
  one time only"; "An HTTP 5xx without definitive command evidence is ambiguous"; "Retry gate
  conformance": Architect AX, Binance, Coinbase, Derive, Hyperliquid and Lighter send order commands once,
  Betfair repeats them under a stable `customerRef`; Bybit, Kraken spot and OKX can send some twice,
  "Unsafe") (fetched, raw).
* https://docs.alpaca.markets/us/docs/working-with-orders.md, FAQ: "What should I do if I receive a
  timeout message from Alpaca when submitting an order? The order may have been sent to the market for
  execution. You should not attempt to resend the order or mark the timed-out order as canceled until
  confirmed by Alpaca Support or the trading team." No status is named (fetched, raw). "Create an Order"
  (`/us/reference/postorder.md`) lists 200, 403 and 422 and calls `client_order_id` "A unique identifier for
  the order" (fetched, raw).
* https://raw.githubusercontent.com/psf/requests/v2.34.2/docs/user/advanced.rst, "Event Hooks": "You can
  also add hooks to a `Session` instance. Any hooks you add will then be called on every request made to
  the session"; an unhandled exception in one "won't be passed silently" (fetched, raw).
* alpaca-py 0.45.0 as installed, `alpaca/common/rest.py`: `RESTClient.__init__` keeps the defaults unless
  `retry_attempts > 0` or `retry_exception_codes` is non-empty; `_one_request` raises `RetryException` for
  a code in `_retry_codes` while retries are left, else `APIError(response.text, http_error)` (read).
  https://github.com/alpacahq/alpaca-py/pull/870, "fix: honor zero retry count and wait options", open,
  not merged, changes `alpaca/common/rest.py` and its test, `tests/common/test_rest.py` (GitHub API,
  fetched).
* Second reader, 2026-11-10 (`evidence-verifier`): the sources hold. Corrected above: a race in the first
  send count, now per call; the search result #211; the disconnect claim, now UNVERIFIED; the hang's cause
  and duration; where the connect-timeout inference comes from; what the contract test checks.
