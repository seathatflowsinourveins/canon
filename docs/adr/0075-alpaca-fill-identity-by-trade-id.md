---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-11; read and accepted by the lead on 2026-11-11
review-by: 2026-12-11
---

# One identity for an Alpaca fill: its trade ID, and no inferred fill for an order the node holds

## Context and Problem Statement

ADR 0042's term of 2026-11-11 ("one identity for a fill") knew a fill by Alpaca's trade ID, and matched a
fill the engine had inferred from a status report to a later stream fill by its shares. The first review
round on that model found the match failing in six ways, all members of one family. NautilusTrader infers
a fill under an ID of its own, so nothing that arrives later can be matched to it by trade ID, and a
match by shares cannot tell fills of different sizes, prices or order apart. Under ADR 0073 a family
found twice is redesigned as one model. This record replaces the matching: the engine is never left to
infer a fill for an order the node holds.

## Decision Drivers

* NautilusTrader deduplicates a fill by its trade ID only: "If the logical fill arrives with different
  IDs, the engine treats the reports as distinct" (execution `index.md`, "Overfills", at the pin).
* The engine infers a fill from a status report that shows more filled than the node's order, for the
  difference (`report.filled_qty - order.filled_qty()` in `reconcile_order_report`, the event built by
  `reconcile_order_report_with_commission`, at the pin); a canceled or expired one it holds back,
  "waiting for fill reports".
* Alpaca lists every fill, with its trade ID, price and time, in its fill activities (ADR 0024), which
  can lag the stream by an amount no page states.
* The trading path needs a failing test first for each finding and a paper run (ADR 0074).

## Considered Options

1. Keep matching inferred fills by shares, and patch each way it fails.
2. Attach the missing fills to the status report (`_handle_report(report, fills)`).
3. Bring the missing fills through the client before any status report shows them, by trade ID, and
   hold a report back until the node's order has applied them.

## Decision Outcome

Chosen: **option 3**, because it is the only one under which every fill the node's order holds carries
Alpaca's trade ID, so the engine's own deduplication works on every source.

* **One identity.** A fill is known by Alpaca's trade ID alone: the stream's `execution_id`, a fill
  report's trade ID and a fill activity read by the client are the same UUID (ADR 0024). An ID the
  order's record or the node's cached order holds is not counted again. The share matching is deleted.
* **No inference for an order the node holds.** When an Alpaca read of such an order shows more filled
  than the node holds (a stream update, a `QueryOrder` answer, a refused submit's read, an open-order
  check), the client reads the fill activities of the order's replace chain and applies the missing fills,
  oldest first, each under its own trade ID, price and time. A status report reaches the engine only once
  the node's cached order shows at least as many filled (an order the node holds closed is reported as
  read); until then it is held back, and the engine's
  next check asks again. While the activities do not list the fills, the report stays held back, with a
  warning. A stream update showing fills the activities do not list has them read again, three times in
  all, three seconds apart, and then leaves the order with an error; a choice, since Alpaca states no
  lag: the wait is alpaca-py's own between attempts of a read (`DEFAULT_RETRY_WAIT_SECONDS = 3`; it
  retries a 429 or 504 `DEFAULT_RETRY_ATTEMPTS`, 3, times after the first attempt). The later reads walk
  the replace chain at Alpaca; an update showing more while they wait gives them their reads again.
* **An end after its fills.** A `canceled` or `expired` update, or such a status report, showing more
  filled than the node holds brings the missing fills first and then ends the order itself, after them
  on the same queue; while the activities lag the end waits on the order's record with the later reads,
  across a reconnect too, and a fill update arriving meanwhile is applied.
* **Counted.** Each catch-up that brings the fills logs which read did ("caught up on read n of 3"),
  and one whose reads run out logs an error naming the order and the shares shown and held, so a paper
  run measures the lag against the three reads.
* **Start-up.** Fill reports go to the engine by trade ID, as before. Each fill reports request also
  reads the open orders. Start-up has no exit to inference for an open order the node does not hold
  that shows fills: its replace chain's fill activities are read at any age, and either its fills are
  reported by trade ID on the open order, outside the engine's window too, or, after three reads, the
  fill reports fail, naming it and why, and start-up with them. Its fills are what its status report
  counts, as the order reports make it: the chain's when they add up to its filled shares, or, under
  the chain reading, its own when only they do; no other count passes, whatever the activities'
  `cum_qty` says, and no later check of the fill reports judges them again.
* **A fill on an order of the chain the node never held** is applied under the Alpaca order the node's
  order is on now, as NautilusTrader's `reconcile_fill_report` applies a fill report under the order's own
  venue order ID when the order does not hold the report's. An order Alpaca shows replaced, in the
  update or a read of it, is never adopted: a fill on a replacement the record has not adopted adopts it
  only when its `replaces` links reach the order the record is on, so a modify's replacement takes its
  quantity before its fills count, and an order the record has moved past keeps the record where it is.

Option 2 was not taken: `reconcile_order_with_fills` at the pin still infers the rest of a continuous
report's filled quantity after its fills, so it removes none of the lag cases, and NautilusTrader's
guide says to "Use `OrderStatusReport` for order lifecycle updates when fill details arrive on a separate
stream", which Alpaca's do.

### Consequences

* Good: Antigravity's findings 1 to 5 and 7, and Codex's history recount, are each fixed by one rule,
  and finding 8's vacuous test goes with the matching it tested.
* Good: a lost stream update no longer books the missing share to an `EXTERNAL` order: the next read
  showing it brings it by its trade ID.
* Bad: a report is held back for one engine check after its fills are brought, and for as long as the
  activities lag. Each fill reports request costs one read of the open orders more, and for each open
  order the node does not hold that shows fills, its chain's reads and activities, on each try.
* Bad: linked to the in-flight-timeout external booking (session window claim 10, ADR 0044). An order
  still `SUBMITTED`, `PENDING_UPDATE` or `PENDING_CANCEL` whose fills the activities do not list through
  all of the engine's in-flight queries is resolved locally: a submit rejected with `INFLIGHT_TIMEOUT`, the
  others canceled (`check_inflight_orders` at the pin). A fill Alpaca makes on it afterwards is booked to
  an `EXTERNAL` order. With `open_check_open_only` off, an open order whose report is held back past
  `open_check_missing_retries` and a targeted query is resolved locally too: an accepted one rejected
  with `NOT_FOUND_AT_VENUE`, a partly filled one canceled, a pending one deferred. How long the
  activities lag is not on Alpaca's pages (UNVERIFIED).
* Neutral: a fill report the engine has not applied yet when the stream's update for the same fill
  arrives is counted once, since the client's record adds a fill to the larger of its own count and the
  node's order's, never their sum, and the engine's order refuses the repeat. The record does not note a
  fill report's trade ID when it makes the report, because the engine, not the client, applies or drops
  the reports it asked for, and an ID noted for a dropped one would refuse that fill from every later
  source.
* Bad: a start-up with an open order whose fills Alpaca never lists fails until they are listed or the
  order is closed by hand. A read of the open orders, or of such an order's chain, that fails on all
  three tries fails the fill reports too, where a failed read of the fill history falls back to
  inference. An order outside the node that closes between the engine's reads of the order reports and
  of the fill reports at start-up has its fills outside the window inferred, as before. Mid-session the
  engine asks for fill reports from its position check (`plan_position_fill_reports` at the pin), which
  an outside order partly filled and not listed fails the same way.
* Bad: an end whose fills the activities still do not list after the three reads ends the order with
  the fills the node holds, with an error; the rest reach the position check as an `EXTERNAL` order,
  as in claim 10's case. A fill on a replacement the node has not adopted costs one read of it when
  its update does not show whether Alpaca replaced it.
* Neutral: at start-up NautilusTrader logs "Generated inferred fill for <id> (<venue order ID>) qty=..." for
  every order it creates from a filled report, and then replaces that fill with the fill reports' by
  trade ID, inferring only what they leave short (`manager.rs` at the pin, the external order's events);
  only the form "Generated inferred fill for <id>: qty=" is an inference the cache keeps. Seen on paper on
  2026-11-11, the order holding Alpaca's trade ID alone.
* Bad: an open order outside the node that fills between the engine's start-up reads of the order
  reports and of the fill reports, which run concurrently, can have that fill inferred, and its stream
  update then counts it again. The window is the time between the two reads.
* Bad: a trade update's own read runs in the task that applies the updates, so the updates behind it
  wait for one read of the activities of each order the record holds (not walked at Alpaca; the later
  reads are). That keeps an end from overtaking the fills its update shows.

### Confirmation

Offline, on a real `LiveNode`, each failing before this change (main at a2042a4) and passing with it,
crediting the reader who found it:

* Antigravity, round 1 on the fill-identity model:
  * finding 3: a `QueryOrder` report and its fill's stream update in one loop turn, FILLED 2 on one share;
  * finding 2: a 2-share fill after a 1-share one, both orders of size, FILLED 4 on 3 shares, or an
    overfill refused while the client ended the order;
  * finding 7: two prices delivered out of order, an average of 150 for fills at 150 and 140;
  * finding 1: a lost update under the paper node's open-order and position checks, open and closed,
    with an engine-made trade ID and, closed, an `EXTERNAL` order;
  * finding 4: a fill on a chain order the node never held moving the order back, and its end ignored
    after a reconnect;
  * finding 5: a replacement's fill after a reconnect under the own reading taken for an inferred share.
* Codex, round 1 on the fill-identity model: a full-history open-order check after a `QueryOrder` report,
  FILLED 3 where Alpaca shows 2, never streamed and streamed.
* This model: a report held back while the activities lag; an open order whose fills are not listed
  failing start-up by name, after three reads; and one listed on a second read reaching start-up under
  its trade ID.

* The code review of this model, rounds 1 to 3, each failing before its round's fix: an open order's
  fill older than the start-up window counted again by its trade ID; an open replacement whose earlier
  orders were outside the window failing start-up; a second update not widening the reads waiting, or
  showing more during the last of them; a trade ID listed at two prices failing start-up under the
  wrong cause; a chain walk cut short by one failed read kept for every try; and open-order fills kept
  between requests reaching the wrong one or outliving a failed check.
* Rounds 4 and 5 found the kept open-order fills, and then the two reads that replaced them, in new
  forms, so the design changed rather than another test being added: each fill reports request reads
  the open orders itself and chooses, checks and reports an outside order's fills in one read, by its
  status report. Their findings are covered by the tests above and have no failing test of their own.
* Outside round 1 on this model (Codex and Muse, findings only), each failing before its fix:
  * Codex 1 and Muse 3, one family, start-up leaving an outside open order's fills to inference: a
    later failed read of the order dropping fills already shown whole; a newest activity without
    `cum_qty`, or counting its own fills only;
  * Codex 2 and Muse 1, one defect: a cancel showing a fill the stream lost ending the order without
    it, listed, listed later, and with the fill's update after the cancel;
  * Codex 3, and Antigravity's upstream-conformance read (item 1): a canceled `QueryOrder` report held
    for its fill and never asked for again under the paper node's checks;
  * Antigravity's upstream-conformance read (item 6), next to Codex 1: the window's end applied before
    the check that an order's listed fills are all of them, dropping the fills inside it;
  * Muse 2: a fill on the middle order of a three-order outside chain moving the node's order back to
    it, the update showing the order as filled or as replaced;
  * Muse 4: the test of a fill listed on a second read now lists it only from the order's second read,
    and fails with one read.
* The code review of the round-1 fixes, each failing before its fix: a fill on a replacement of
  raised quantity, before its `replaced`, ending the order early; a cancel waiting for its fill lost
  across a reconnect; an outside order pending replace and its replacement both reporting one fill.
  Declined: a late fill on an order the node's order held does not move it back
  (`nautilus-trader@d53849b216e2:crates/model/src/orders/mod.rs:1389-1395`).
* The lead's question before landing: a fill report and its stream update in one engine step, in
  either order and with another fill between them, counted once; a guard that passed before the question
  and fails when the record sums its own count and the order's.

On paper, 2026-11-11, regular session, account A for the tests and B for the window:

* At cb1562f, 09:33 New York: `mise run paper:test -- -s -v -k
  "test_paper_fill_activity_ids_carry_the_execution_ids or test_paper_startup_reconciliation_reports_alpacas_fills"`,
  exit 0, 2 passed (log `paper-runs-1111-item1-tests-rerun-093349.log`); a run at 09:31 failed with the account
  left flat, paper not making a 2-share market order final within 60 s at the open.
* At e5a02b4, 09:46 to 09:58: `mise run paper:test -- -s -v -k test_paper_exec_tester_market_and_limit_buy`,
  exit 0, 1 passed, two fills through the node, each one `OrderFilled` under Alpaca's trade ID, flat
  (`paper-runs-1111-repeat-matrix-094641.log`); the two fill tests again, exit 0, 2 passed
  (`paper-runs-1111-repeat-filltests-094732.log`); `mise run paper:session --session regular --minutes 10`,
  exit 0, warmed up, no cross and no order (`paper-runs-1111-repeat-window-094740.log`).
* Every activity's ID carried the stream's `execution_id` and its time, to the microsecond; each order
  reconciled at start-up held one `OrderFilled` under that ID.
* The three-reads question, first answer: no catch-up episode in the 2 fills through the node today
  (caught up on read 1, 2 or 3: 0; ran out: 0; held back: 0; inferences the cache kept: 0). Every fill
  came on the stream, and paper gives no way to lose an update on demand, so the catch-up path itself
  did not run on paper and the lag stays unmeasured.

Review rounds (ADR 0073, rule 5): round 1 on the fill-identity model found 9 behaviour-changing findings,
most of them in the known family; outside round 1 on this model found 5 behaviour findings and 1 vacuous
test (Codex 3, Muse 4, one shared), with Antigravity's upstream-conformance read folding 1 more into its
families; `/code-review high` ran five rounds on the model and one on the round-1 fixes.

Finding 6 (a replacement's end before a fill on the order it replaced) is the replace chain's own item,
fixed on its own branch after this one. Observed while building: a report returned to the engine was
reconciled before the fills generated just before it, and the engine inferred a fill; so a report waits
for the node's cached order.

## Evidence

Read 2026-11-11.

* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/index.md
  — "Overfills": deduplication by `trade_id`, and distinct reports for one fill under different IDs —
  fetched (raw).
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/live/src/execution/manager.rs
  — `reconcile_order_report` (the inferred fill; "terminal report ... has unaccounted fills; waiting for
  fill reports"), `reconcile_order_with_fills` (fills, then the report's rest), `check_inflight_orders`
  (`INFLIGHT_TIMEOUT`, and a local cancel past the last retry), `reconcile_open_order_reports` and the
  missing-order resolution — fetched (raw), at the commit `scripts/canon` resolved.
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/execution/src/reconciliation/orders.rs
  — `reconcile_fill_report`: a fill under a venue order ID the order does not hold is applied under the
  order's own — fetched (raw).
* https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/reconciliation.md
  — "Use `OrderStatusReport` for order lifecycle updates when fill details arrive on a separate stream" —
  fetched (raw), the copy kept from ADR 0042's term.
* https://github.com/alpacahq/alpaca-py/blob/v0.45.0/alpaca/common/constants.py — `DEFAULT_RETRY_ATTEMPTS
  = 3`, `DEFAULT_RETRY_WAIT_SECONDS = 3` — bundled (read in the installed 0.45.0).
* [ADR 0024](0024-alpaca-fill-history.md): the stream's `execution_id` is a fill activity's trade ID,
  observed on paper.
* The two readers' reports of round 1 on the fill-identity model at beba468, Antigravity (8 findings) and
  Codex (1): each reproduced here by a failing test before the change.
* Corrected 2026-11-11, by the `evidence-verifier` (two passes): the inferred fill's source, alpaca-py's
  retry count, the missing-order resolution, the position check as the mid-session caller of fill
  reports, the cost of the open-order read, and the round 5 Confirmation.
* Alpaca's fill activities' lag behind the stream: not on Alpaca's pages — UNVERIFIED. The gap this record
  leaves open is how long the lag lasts in the regular session: no catch-up ran on paper on 2026-11-11, so
  the count lines (Decision) are how a later window measures it against the three reads.
