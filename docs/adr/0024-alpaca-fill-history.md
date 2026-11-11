---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), applying this record's rule under the owner's delegation
review-by: 2026-12-10
---

# Fill history for the Alpaca adapter: activities through alpaca-py's `get`, keyed by `execution_id`

## Context and Problem Statement

`AlpacaExecutionClient` returns no fill reports, so NautilusTrader infers the fills of every order it
reconciles (ADR 0030, execution slice). The next slice is meant to report each execution. alpaca-py 0.45.0's
`TradingClient` has no call for Alpaca's account activities, and rule 3 of ADR 0001 allows only an upstream
API called as documented. Where does per-execution history come from, and may the adapter use it yet?

## Considered Options

1. Keep inferred fills.
2. Call `GET /v2/account/activities` through `TradingClient.get`, alpaca-py's generic request method.
3. Wait for alpaca-py to wrap the call, then move the pin.
4. Ask upstream, for the call and for what an activity's `id` is.
5. Another documented source: Alpaca's activity events stream, `GET /v2beta1/events/activities`.

## Decision Outcome

Accepted: **option 2, with `execution_id` as the trade ID on both paths, kept behind its paper test.
Option 3 replaces option 2 when a release carries the call.** The paper test below matched every time on
2026-11-10 (Confirmation), which is this record's own rule for accepting it, applied under the owner's
delegation of that day. Until the slice is built, option 1 stands.

**The endpoint.** Alpaca's Trading API reference documents `GET /v2/account/activities`, and a by-type
form, with the paper server listed. Parameters: `activity_types`, `category`, `order_id`, `date`, `until`,
`after`, `direction` (default `desc`), `page_size`, and `page_token`, the `id` of the last activity read. A
fill has `id`, `activity_type` (`FILL`), `type` (`fill` or `partial_fill`), `transaction_time`, `price`,
`qty`, `side`, `symbol`, `order_id`, `cum_qty`, `leaves_qty` and `order_status`: no commission, no
liquidity side. The order endpoint gives totals only.

**Where Alpaca's pages disagree.** With `date` set, the guide says there is "no maximum page size"; the
reference caps `page_size` at 100. The guide lists `TRANS` and `MISC` as activity types and its fill has no
`order_status`; the reference calls the two filter aliases and has the field. The reference filters `after`
and `until` on when an activity was created, and says `created_at` is "Null for trading activities", so
what they compare for a fill is unstated. The adapter would follow the reference.

**The identity question, as the documents leave it.** The client's trade ID is the stream's
`execution_id`, which Alpaca shows only in examples, as a bare UUID. An activity's `id` is "Always in
`::` format"; alpaca-py's docstring calls its halves a date string and a UUID. No Trading API page says that UUID is the `execution_id`. Alpaca's
Broker guide, for Broker endpoints only, points both ways: its field tables map the REST `id` ("Renamed.")
and the trade events' `execution_id` ("Renamed. Same value (execution UUID).") to one `ref_id`, yet it also
says "No common stable ID across SSE and REST". A match is plausible, not documented. NautilusTrader's
pinned docs: "If the logical fill arrives with different IDs, the engine treats the reports as distinct." So
a fill taken from the stream and again from a report under another ID counts twice where it fits the order's
quantity; elsewhere it is rejected as an overfill (`allow_overfills` defaults to false), unless it is a
standalone reconciliation fill, which "may raise quantity first".

**The test that settles it** (`packages/canon-alpaca/tests/test_paper_fill_ids.py`): place a paper order
that fills, in parts if possible; record each fill update's `execution_id`; read the order's `FILL`
activities; compare every `id`, and the UUID after `::`, with them. A match every time: accept this record
with `execution_id` as the trade ID on both paths, and keep the test, since the match is observed, not
documented. No match: option 2 has no shared ID and is dropped. The result is under Confirmation.

**Each option against rule 3.**

1. *Inferred fills* meet it: nothing new is called. NautilusTrader "Infers `OrderFilled` events for missing
   trade reports", timed at the report's `ts_last`, so per-execution prices and times are lost. The pinned
   page does not say what price an inferred fill carries.
2. *The generic `get`.* The endpoint meets it. The method meets it on one reading: `RESTClient.get` is
   public in the pinned source, has a docstring ("Performs a single GET request") and is what every wrapped
   `TradingClient` read calls, but alpaca-py's README and docs never mention it. This record, accepted,
   counts that as documented. The adapter would own the query, paging and parsing: 0.45.0's
   `TradeActivity` requires an `account_id`, which the Trading reference's fill does not have.
3. *Waiting* meets it fully, at no known date. No release is newer than v0.45.0. Pull request 845 added
   the call as `self.get("/account/activities", params)`; it was closed unmerged on 2026-11-10 by the
   author of draft 906, who wrote that 906 supersedes it. Pull requests 324 and 646 for it closed
   unmerged. Draft 906, opened 2026-11-10 by an author with commits on `master`, adds the call and the
   events stream, among 18 open `release-0.50.0` pull requests.
4. *Asking* is no code and the owner's to do. Issues 232 (open since 2023-01-09) and 595 ask for the call;
   their comments hold no maintainer reply.
5. *The events stream* is documented, paper server listed, and its `ref_id` is, for trades, "the
   execution_id". It does not meet the rule today: it is server-sent events, which `RESTClient.get` cannot
   read (it returns `response.json()`) and 0.45.0's `TradingClient` does not wrap, and the Broker guide says
   its beta path "may change". No page says `ref_id` is the `trade_updates` stream's `execution_id`.

### Consequences

* Good: a fill from the stream and the same fill from a report would carry one trade ID, so the engine
  would not count it twice; the identity rests on a kept paper test.
* Bad: the match is observed on paper, not documented, and a live account is not tested (ADR 0009); option 2
  is code a later alpaca-py release makes redundant.

### Confirmation

**Result of the paper test, 2026-11-10.** Six runs, each a market buy of 2 AAPL and a market sell of 2,
so twelve orders and twenty fills, eight of the orders in two parts. Every activity was one fill update:
its `id` was `<17 digits>::<that update's execution_id>`, never the bare UUID, with the same `qty`,
`price` and `type` (`fill` or `partial_fill`), and its `transaction_time` the update's `timestamp` or 1 µs
later. The 17 digits read as that time in New York, truncated to the millisecond. Alpaca's paper page says
partial fills come "10% of the time"; eight of twelve orders here is far above that, and no page read
explains the difference.

`scripts/canon lint` checks this record. `mise run paper:test` runs the paper test with every other paper
test (alone: add `-s -k test_paper_fill_activity_ids`); it skips outside the regular session. A run
without a match reopens this record. The slice is built (2026-11-10): `execution.py`'s header names
Alpaca's reference page and `RESTClient.get`, its fill reports take the trade ID from the UUID after `::`,
and `test_paper_fill_reports.py` checks on paper that start-up reconciliation gives a filled order the
stream's trade IDs and prices. An order's reports are kept only when they can be shown to be all
its fills, whatever their age: its newest activity leaves nothing to fill (`leaves_qty` 0) and they add
up to its `cum_qty`, or else Alpaca's order shows no more `filled_qty` than they add up to. Otherwise,
and when the history cannot be read (a body that is not JSON or a model alpaca-py cannot build
included), that order's reports, or all of them, are dropped with a warning and the engine infers the
fills; a full page whose last activity has no `id` ends the read there. This replaces a one-minute age
limit on the order check, which no Alpaca page and no observation here supports (the one lag seen on
paper was about a second). It costs, for the orders whose newest listed fill leaves shares to fill or
lacks either field, one order request for one such order, and for more one read of the account's
newest 500 orders plus one request per order not among them. Upstream's OKX and Kraken spot
adapters mark the mass status incomplete instead; NautilusTrader's Python `ExecutionMassStatus` at the
pin has no setter for that, so dropping the fills is a departure from them. `test_execution_client.py` covers each case. `scripts/canon
outdated` reports a newer alpaca-py tag; read its notes for the call before moving the pin. Still
UNVERIFIED: the paging limits and the `after`/`until` filters for fills. Open, and the owner's: any
question to upstream.

## Evidence

Read 2026-11-10. "raw" means the page's markdown or source fetched with `curl` and read, not summarised.

* alpaca-py 0.45.0 installed under `.venv`, and https://github.com/alpacahq/alpaca-py at tag `v0.45.0`
  (cloned; `c42add7c0a70`, the registry's pin): `common/rest.py` (`get`, `_one_request`), `trading/client.py`
  (no activities method), `trading/models.py` (`BaseActivity`, `TradeActivity`), `broker/client.py`
  (`get_account_activities` calls the Broker API's `/accounts/activities`), `README.md`, `docs/` (fetched, raw).
* https://alpaca.markets/sdks/python/api_reference/trading/trading-client.html: `TradingClient` and
  `__init__` only (fetched, raw).
* https://github.com/alpacahq/alpaca-py/releases, pull requests 845, 906, 324 and 646 and issues 232 and
  595, through the GitHub API: states, dates, reviewers, comments and both diffs (fetched, raw).
* https://docs.alpaca.markets/us/reference/getaccountactivities-2.md and
  https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md: path, servers,
  parameters, the fill schema, `page_size` maximum 100 (fetched, raw).
* https://docs.alpaca.markets/us/docs/account-activities.md: the guide's fill, its paging text (fetched, raw).
* https://docs.alpaca.markets/us/changelog/2026-11-06-activity-types-b66d603.md: `TRANS` and `MISC` are
  filter values only (fetched, raw).
* https://docs.alpaca.markets/us/docs/websocket-streaming.md: `execution_id` in examples only (fetched, raw).
* https://docs.alpaca.markets/us/reference/subscribetoactivitiessse.md: the events stream, its servers,
  `since` and `until`, `ref_id` (fetched, raw).
* https://docs.alpaca.markets/us/docs/activity-sse.md: the Broker guide; "may change"; "No common stable ID
  across SSE and REST"; the field tables that map `id` and `execution_id` to `ref_id`; its checklist asks
  for "matching `ref_id`" across old and new events (fetched, raw; read in parts, not whole).
* https://docs.alpaca.markets/us/reference/getorderbyorderid-1.md: an order carries no list of executions
  (fetched, raw; searched, not read whole).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/reconciliation.md
  ("Startup reconciliation", "Fill reconciliation", "Event ordering"), `docs/concepts/execution/index.md`
  ("Overfills", "Duplicate fill detection") and `docs/developer_guide/python_adapters.md` ("Generate
  reconciliation reports") at the same commit, the pin (fetched, raw). The `latest` page the adapter cites
  now adds "unless the client requires authoritative fills" to the inference line; the pin does not.
* `upstream-scout`, 2026-11-10: read the same Alpaca pages through a summariser and found pull request 906
  and the events stream; each of its findings used above was re-read raw. It found no official statement
  tying a Trading API activity `id` to `execution_id`; the Alpaca forum was not searched (UNVERIFIED).
* Second reader, 2026-11-10 (`evidence-verifier`): the claims hold against raw reads and `proposed` follows.
  Corrected above: the Broker guide's field tables, left out of the first draft; the overfill exception;
  where "may change" and "date string" come from.
* `test_paper_fill_ids.py` through `mise run paper:test`, 2026-11-10, New York time: three runs at 13:52
  and 13:53, one at 14:16 once the test also compared `qty`, `price` and `type`, one at 14:19 and one at
  14:24 after review changes to the test; each passed, with the observations above, printed by the test
  (run). A run at 14:19 stopped on a defect in the test before any fill, its buy canceled unfilled, and
  compared nothing. Before each run the test found no open order and no position; each run's orders
  filled and netted to zero shares.
* https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1.md — the `order_id` filter,
  "Useful for retrieving the fills that make up a completely filled order" (fetched, raw; 2026-11-10).
* https://docs.alpaca.markets/us/docs/paper-trading.md — paper orders "receive partial fills for a random
  size 10% of the time"; eight of twelve orders here filled in parts (fetched, raw; 2026-11-10).
* https://github.com/alpacahq/alpaca-py/pull/845 through the GitHub API, 2026-11-10: closed unmerged at
  16:52 UTC, and the closing comment (fetched, raw).
* Second reader of the acceptance, 2026-11-10 (`evidence-verifier`): the match and the rule hold against
  the run logs. Corrected above: the time to 1 µs, "truncated", the partial-fill rate set against Alpaca's
  10%, pull request 845's state, the command that runs the test, "would" in Consequences, and the rule-3
  reading no longer listed as open.
* Not run: the paging limits and the `after`/`until` filters, which stay UNVERIFIED.
* The completeness rule, 2026-11-10: alpaca-py 0.45.0 as installed, `RESTClient._one_request` returns
  `response.json()` for a 200; requests 2.34.2's `JSONDecodeError` subclasses `OSError` and `ValueError`,
  and pydantic-core's `ValidationError`, which alpaca-py's models raise, subclasses `ValueError` (run).
  https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/reconciliation.md,
  "Mass-status history contract" ("An adapter can still return authoritative active orders and position
  reports when a historical source fails. It marks the mass status incomplete"), and at the same commit
  `crates/adapters/okx/src/execution.rs` and `crates/adapters/kraken/src/execution/spot.rs`, whose mass
  status calls `set_report_window(start, complete)` (fetched, raw). The installed rc8's Python
  `ExecutionMassStatus` exposes `reports_complete` with no setter and is built from
  `(client_id, account_id, venue, ts_init, report_id=None)` (run). The reference's `cum_qty`,
  `leaves_qty` ("The quantity of shares remaining to be filled") and `page_token` descriptions, from the
  page above.
* A second reader after landing, 2026-11-10: an activity listed twice counted twice toward its order's
  `cum_qty`, so a repeat could hide a missing fill, and was reported twice. Reproduced with a test and
  fixed: one fill per trade ID, as NautilusTrader's order "enforces one quantity-bearing fill per
  `trade_id`" ("Duplicate fill detection", `docs/concepts/execution/index.md` at the pin, fetched, raw);
  one trade ID with two quantities or prices leaves the order's fills to the engine. Alpaca's pages
  neither promise nor rule out a repeated row, paging by `page_token`, "the ID of the end of your current
  page" (`docs/account-activities.md`, fetched, raw): UNVERIFIED.
