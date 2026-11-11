---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-11
---

# The session window's account check after the stop: one set of unsettled requests, a close under a client order ID of its own

## Context and Problem Statement

ADR 0044 runs `SessionMaCross` on paper in bounded windows and requires each to end flat or fail naming
what is left. After the node stops, an order's answer may have been lost, an order may still be working
and a position may still be held. How does the window decide the account is flat, and how does it close
what is left without sending a close twice?

## Decision Drivers

* Alpaca's guidance on a submit that timed out: the order may have been placed, and it is not to be
  resent until confirmed "by Alpaca Support or the trading team" (Evidence). The check takes Alpaca's own
  answer under the order's client order ID as that confirmation, which the guidance does not name; where
  none comes, it sends nothing more and fails the window.
* No silent fallback (AGENTS.md rule 5): a window not shown flat is recorded failed, naming what is left.
* The check's requests are counted, so a window and its end fit the paper lock's wait (ADR 0044, "Sharing
  the account").

## Considered Options

1. `close_position`, with a lost close searched for among the symbol's orders since it was sent and
   matched by side, quantity and type: the check as it stood before the four independent reviews of the
   check as landed, twelve claims on the window, on 2026-11-10 (Confirmation).
2. One set of unsettled requests, each known by its client order ID, and a close sent as a market order
   under a client order ID of the check's own.

## Decision Outcome

Chosen: **option 2**, split from ADR 0044 on 2026-11-11 with no change of decision. In ADR 0044's table:
after the node stops, read Alpaca and fail naming what is left (each poll for 60 s: look up each request not
yet seen ended, cancel every order that works but the check's own close, again until Alpaca shows it ended;
once none works and while Alpaca's clock says the session is open, close with a market order under a client
order ID of the check's own, and again only once Alpaca has refused the last or shown it ended; cancel a
close that would only queue).

**The account check after the stop** asks one question: which requests does this window not yet know the
end of, and has Alpaca shown each ended? They are one set: each order of the node's that the node did not
see end (sent with no answer, still open in its cache, or ended only by its own reconciliation), and each
close the check sends. One leaves the set only on Alpaca's word: an order under its client order ID in a
status after which "no further updates will occur" (`filled`, `canceled`, `expired`, `rejected`), or, for
a close, Alpaca's refusal of the request (403, 422) or a connect timeout that sent nothing. Flat is no
open order, no position and the set empty.
The close is a market order for the held quantity under a client order ID of the check's own, not
`close_position`, which takes none: an order "can be queried using either the client-provided order ID or
the system-assigned unique ID", so which order is the check's, and whether a close whose answer was lost
was placed, are read, not inferred from side, size and type. **"Never placed" is concluded only from
Alpaca's refusal or from a request that was never sent.** A lookup that finds nothing is not that: Alpaca
documents no time by which a submitted
order shows in a lookup or a listing, and of a submit that timed out says "The order may have been sent
to the market for execution. You should not attempt to resend the order". So while a close, or a node
order on the close's side, is sent and not found, no other close is sent; never found, the window is
recorded failed naming the position and the request, and the next refuses to start. Departure from
`close_position`: a market order is not refused for a position that has gone (it would sell short), so
the close is sent only in a poll that has read no order working and no such request.

Every request of the check is counted: a reading looks up at most two requests (`LOOKUPS`), each in turn
over the polls, and a step cancels at most two orders (`CANCELS`); what is not reached in the check's time
is named in what is left.

### Consequences

* Good: a close whose answer was lost is looked up by its own ID, never resolved by sending another.
* Bad: a close or a node order that is never found fails the window; the next window starts only once
  Alpaca shows no open order and no position in the symbol (ADR 0044, ADR 0050), so a request never found
  with nothing held does not stop it.
* Bad: what Alpaca answers a lookup of an ID it does not hold, and how soon a submitted order shows in a
  lookup or a listing, are not documented (UNVERIFIED, Evidence).
* Bad (2026-11-11): the check cancels every open order in the window's symbol that is not its own close,
  whoever placed it, so until the check is limited to the window's own lineage (client order IDs, as
  `src/canon_trading/custody.py` names them), nobody places an order by hand in a symbol a window is
  trading. `mise run paper:account <tests|windows>` shows each open order as ours or foreign.
* Neutral (2026-11-11): what a window leaves of ours (a node killed before its check ran) is closed by hand
  with `mise run paper:close <tests|windows>`, under the account's lock: it cancels and closes only what our
  lineage wrote and the quantity our orders opened, one attempt each, and freezes and names a symbol with
  anything else in it. Its closes carry `canon-close-hand-`, of the lineage this check's closes carry.

### Confirmation

Offline, in `scripts/ci` (`tests/test_session_window.py`, against one stand-in for Alpaca's paper Trading
API). Earlier reviews of the window, which also changed the check, are in ADR 0044's Confirmation.

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

Four independent reviews of the check as landed (2026-11-10), twelve claims on the window, each first shown
by a failing test in the same file, those on the check against one stand-in for Alpaca's API and all
answered by the one set of unsettled requests above, which replaces the search for a lost close among the
orders since it was sent and its match by side, quantity and type:

* A second close went out after two searches had not found the first, whose answer was lost: both could
  sell (a short, bought back, recorded ok with no trace), and a first that arrived after the process had
  gone was never resolved. No close is now sent while one is sent and not found, and each close and each
  unsettled node order is in the record with what Alpaca showed of it (`closes`, `unsettled`).
* A lost close found working after another order had flattened the account was dropped from the check:
  it is tracked by its ID and canceled when it no longer closes what is held.
* The bounds did not count a lookup for each order in doubt nor the search (twelve orders made the check
  420 s): a reading now looks at most two up and a step cancels at most two, every request is counted,
  and two tests put the last close and the check's end exactly at their bounds, every request, the
  position read included, taking its full timeout.
* A close sent in the last poll was in no reading when the reading after it failed, and stayed queued; an
  order found by its lookup in a poll whose listing then failed was never canceled; one canceled in the
  last poll was named working after it from a list the poll had kept: the last cancels go to every order
  the check knows of, and an order Alpaca has shown ended is in none.
* A clock that could not be read as the check ended was taken for a closed session, and the check's
  working close canceled: the clock's last answer, open until the close it gave, now stands.
* `close_position`'s 404 for a position another order had just closed was taken for a lost close, a flat
  account recorded failed: there is no such call now, and no close while the node's own exit may fill.
* A skip whose heartbeat could not be written exited 1 with a traceback: it now says why and exits 0.
* A Ctrl-C during the wait for a worker call after a clean stop left the runner out of the abandoned, so
  the node was disposed under a running thread and the process hung at exit: it is abandoned first.
* The time left was measured from before the clock read, so that read's own retries were taken from a
  window whose end is the timestamp it returned: it is measured from the clock's answer. The same claim's
  other half is not changed: a host clock ahead of Alpaca's ends a window early, which its record shows
  (`seconds` under `planned_seconds`); the 60 s allowance is for one behind, and nothing compares the
  host's clock with Alpaca's (that the host keeps time is assumed, UNVERIFIED).
* The bound's tests stubbed the position read at no time and held no order in doubt: the stand-in charges
  every request, and refuses, fills and answers as Alpaca's documentation and error guide describe.

Code review (`/code-review`, high) of that change: a connect timeout on the close's only attempt, which
sent nothing, was kept as a close with no answer and held every later close back (now settled, as the
adapter's `not_sent` reads it); the last cancels were cut to two (now every working order, while the step
has time); an order the node had ended by its own reconciliation was not looked up (now any order with a
`reconciliation` event is); an answer alpaca-py could not parse ended the check; an order listed open
while its lookup answered 404 was named not found; a refused close added a record entry each poll (now
counted, `closes_not_placed`); the account was read twice with nothing sent between; the adapter's
refusal statuses and page limit were copied, not imported; each fixed, the first six covered by a new
test. Left open then: ADR 0044 was past the length its rule sets, and the account check was the part to
move to a record of its own; it moved here on 2026-11-11.

Also changed with them: `replaced` no longer settles an order (Alpaca does not say it gets no further
updates; no window strategy replaces one), every node order still open in the node's cache, or ended by
the node's own reconciliation, is looked up as one in doubt is, and the open-orders listing asks for 500,
its maximum. Not shown on paper: all of this
is offline until a window's stop leaves something to clear.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* alpaca-py 0.45.0 in `.venv`, `common/rest.py` and `common/constants.py`: `RESTClient._request` sends a
  request answered 429 or 504 again, `DEFAULT_RETRY_ATTEMPTS` 3 times, `DEFAULT_RETRY_WAIT_SECONDS` 3 apart,
  from the count it keeps at `_retry`; `TradingClient.__init__` passes no `retry_attempts` (`bundled`).
* Python 3.14's `asyncio` in the toolchain: `set_default_executor` takes a `ThreadPoolExecutor`,
  `to_thread` runs in the default executor, `Runner.close` waits for it at most 300 s (`THREAD_JOIN_TIMEOUT`);
  `concurrent.futures`' `shutdown(cancel_futures=True)` and `wait(timeout=...)` (read; a test runs both).
* 2026-11-11, the second reader (`evidence-verifier`) of the split: the decision unchanged and no text
  lost; corrected here: the two sources above copied with the text that rests on them, Option 1 named
  exactly, the guidance's own condition for a resend stated, and what the next window checks (read).
* 2026-11-11: split from ADR 0044 with no change of decision; the decision text, the reviews of the check
  and the sources below moved here word for word, but for the sentence on the record's length, and ADR
  0044 points here. Two sources stay in ADR 0044 too, since text there rests on them (read).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/tutorials/lighter_rwa_composite_mm.md:
  the scoped reconciliation, start flat, confirm flat (fetched, raw).
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
* 2026-11-10, a second reader's correction (four independent reviews of the check, twelve claims on the
  window, all reproduced offline): the decision's account check, its row in the table and the lock's
  arithmetic are rewritten in ADR 0044, and the check since moved here; `AFTER_END_SECS` 605 to 725, `LAST_CLOSE_SECS` 394 to 455, `END_MARGIN_SECS` 455 to
  515, `LONGEST_WINDOW_SECS` 3076 to 2955 (run).
* https://docs.alpaca.markets/us/docs/working-with-orders.md, "Using Client Order IDs": a
  `MarketOrderRequest` with `client_order_id`, then `get_order_by_client_id`; "FAQ", "What should I do if I
  receive a timeout message from Alpaca when submitting an order?": the two sentences quoted above, and
  "Before taking any action on the timed-out order you should check the broker dashboard and contact
  Alpaca support" (fetched, raw).
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, first paragraph: the sentence on querying by
  either ID; "Order Lifecycle": `replaced` is "replaced by another order, or was updated due to a market
  event", with no word on further updates (fetched, raw).
* https://docs.alpaca.markets/us/reference/postorder.md: `client_order_id`, "A unique identifier for the
  order. Automatically generated if not sent. (<= 128 characters)"; responses 200, 403 "Buying power or
  shares is not sufficient", 422 "Input parameters are not recognized", nothing on a repeated ID.
  https://docs.alpaca.markets/us/reference/getorderbyclientorderid.md: only a 200 is documented, so the
  404 for an ID Alpaca does not hold stays UNVERIFIED.
  https://docs.alpaca.markets/us/reference/deleteopenposition-1.md: `qty` and `percentage` only, no
  client order ID; "Returns the order created to close out this position"; neither the order's type nor
  a 404 is documented. https://docs.alpaca.markets/us/reference/getallorders-1.md: "Defaults to 50 and max
  is 500" (each fetched, raw). No page read states when a submitted order becomes visible to a lookup
  or a listing.
* https://docs.alpaca.markets/us/docs/user-protection.md, "Preventing Wash Trades": a market order
  against an open order of the other side is "always rejected", with a 403, on paper too (fetched, raw).
* https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca, Alpaca's own error guide,
  undated (fetched, raw, read as text): "client_order_id must be unique (HTTP 422 Error) ... a duplicate
  client_order_id was used for another active order", so a repeat of an ID is no documented way to send a
  close once only; "Insufficient qty available for order (HTTP 403 Error)": shares an open sell holds
  "cannot be used again", and after a cancel "wait for confirmation that the open orders are actually
  canceled before sending the close request". The test stand-in's refusals follow these two pages.
* alpaca-py 0.45.0 in `.venv`, `trading/requests.py` and `trading/client.py`: `OrderRequest.client_order_id`,
  `MarketOrderRequest`, `submit_order` returning the `Order`, `ClosePositionRequest` with `qty` and
  `percentage` only (`bundled`).
* `crates/execution/src/reconciliation/orders.rs` at the pin: `create_reconciliation_rejected` and
  `create_reconciliation_canceled` build their events with `true, // reconciliation` (fetched, raw);
  nautilus-trader 2.0.0rc8 in `.venv`, `model/__init__.pyi`: `OrderRejected.reconciliation`,
  `OrderCanceled.reconciliation` (`bundled`). `packages/canon-alpaca/src/canon_alpaca/providers.py`,
  `counted` and `not_sent`: a connect timeout on a call's only attempt sent nothing (ADR 0039) (read).
* Code review (`/code-review`, high), 2026-11-10, of the account check as one set: ten findings, eight
  reproduced against the tests' stand-in and fixed above, one (a second record of the check's own
  closes) removed, one (ADR 0044's length) left open then, closed by this split (run).
