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
3 times, 3 s apart; a timeout is not an HTTP status, so it is not repeated. One attempt that hangs now ends
within about 30 s; a call repeated three times after 429s can take about four attempts' time plus 9 s. The
paper node's disconnect timeout is 15 s, shorter than the read timeout, so a call hanging when the node
stops can still outlast the disconnect; that the run then ends once the read timeout fires, rather than
never, follows from the request ending, and is UNVERIFIED on a stopping node.

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
hangs. It also covers each outcome above. On paper, the execution rows of `test_paper_matrix.py` run with
the adapter at the next regular session (UNVERIFIED until then). Before moving the alpaca-py pin, read its
notes for a timeout option and for `RESTClient`.

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
* Second reader, 2026-11-10 (`evidence-verifier`): the sources hold. Corrected above: a race in the first
  send count, now per call; the search result #211; the disconnect claim, now UNVERIFIED; the hang's cause
  and duration; where the connect-timeout inference comes from; what the contract test checks.
