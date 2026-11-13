---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Modify for the Alpaca adapter: Alpaca's replace, updated only when Alpaca confirms it

## Context and Problem Statement

`AlpacaExecutionClient` (ADR 0030, ADR 0036) had no `_modify_order`. Alpaca changes an order by replacing it
with a new one under a new ID. How does the client modify an order, and on what does it rely that Alpaca's
pages leave open?

## Decision Drivers

* ADR 0001, rule 3: each behaviour traces to the guide, an upstream adapter at the pin, or an Alpaca or
  alpaca-py page; upstream is read for shape, not copied. Paper only (ADR 0009). What no page states is not
  guessed into code: it is `UNVERIFIED` and gets a paper test.

## Considered Options

Modify: (a) `OrderUpdated` from the PATCH answer, as Binance spot's HTTP fallback does; (b) `OrderUpdated` on
Alpaca's `replaced`, with the replacement's values, as Binance spot's WebSocket path waits for the venue's
stream; (c) not implemented, with a paper test to settle what the pages leave open.

## Decision Outcome

Chosen: option (b).

Alpaca's "Replace Order by ID" answers "The new Order object with the new order ID", says a success "does
NOT guarantee the existing open order has been replaced", and gives the replacement's client order ID as "A
unique identifier for the new order. Automatically generated if not sent." It leaves unstated which order
`replaced` carries, whether the original ID may be kept, and any error status. A paper observation on
2026-11-10, 3 of 3 runs, settled those: a kept ID is refused, 422 "client_order_id must be unique"; the
answer has a new ID, an Alpaca-made client order ID and `replaces`; `replaced` carries the old order with
`replaced_by`; a second replace of the old order is refused, 422 "order already replaced"; no replacement
sent `new`. On that, the master orchestrator chose (b) under the owner's delegation of 2026-11-10.
`_modify_order` sent no client order ID (since 2026-11-12 it sends a new one of the order's own form, ADR
0129; nothing else here changes); `OrderUpdated` comes only with `replaced`, its values from the
replacement as the answer or a read of `replaced_by` gives it, nothing if neither does. Each node order's
replace chain is one record, as Binance spot's `OrderBinding`: built from the node's cache, it holds the
Alpaca orders the order has been, the one it is on and the replaces pending, and every update and report
is judged through it. A replacement is the node's by the record or the cache, else by a walk of `replaces`
of at most 16 reads. A 422 is `OrderModifyRejected` unless Alpaca shows the order replaced as asked
(an earlier replace took it; each replace is sent once, ADR 0039); any other status leaves the outcome unknown. `order_replace_rejected` and the
`rejected` of a replacement that has not taken over are `OrderModifyRejected` too. One that took over
(the order it replaces shows replaced by it) ends the order: rejected, or canceled once partly filled, as
Binance spot's `reject_replace` emits `OrderCanceled`. Denied before Alpaca: no venue order ID, a fractional quantity, a sub-penny
price, a price the order type lacks.

**Added 2026-11-11: one identity for a fill.** A fill counted twice was found three times in the record's
fill accounting, each time from a different source reaching the engine first, so it is settled as one rule
(ADR 0073's practice for a defect family) rather than a fourth point fix. A fill is counted once, whichever
source brings it first. It is known by Alpaca's execution ID wherever Alpaca carries it, the stream's
`execution_id` and a fill report's trade ID being the same UUID (ADR 0024): an ID the record or the node's
cached order holds is not counted again, whatever either count shows. The engine makes an ID only for a
fill it infers from a status report, so the client keeps the trade IDs Alpaca gave it, and the order's
shares under any other ID are inferred: a stream fill under an ID not yet seen is taken for one of them,
on any order of the chain, while any are unmatched, and otherwise is a fill of its own. Alpaca's
cumulative `filled_qty` on the update only narrows that.

**Added 2026-11-11: superseded by ADR 0075.** The share matching in the term above is replaced: a fill is
known by Alpaca's trade ID alone, and the engine is never left to infer a fill for an order the node
holds, since the first review round on the matching found it failing six ways (ADR 0075).

### Consequences

* Good: `OrderUpdated` waits for Alpaca's own confirmation.
* Bad: a modify of an order in a state Alpaca says "cannot be replaced" (`accepted`, `pending_new`,
  `pending_cancel`, `pending_replace`) is refused by Alpaca, not before it, since the client cannot know
  Alpaca's state.

### Confirmation

Paper, 2026-11-10, `mise run paper:test`, all passing. TC-E30 ("Modify limit BUY price") of upstream's
Execution Testing Spec, through its `ExecTester` on the spec's node setup: the tester
amends "one tick more passive", 0.0001 here, so the client denies it as sub-penny, `OrderModifyRejected`: the
row's pass criterion, `OrderUpdated`, fails by design, as the card's "different valid price" is not valid at
Alpaca. Modify through a node, 6 of 6 runs: $1.00 to $1.01, `OrderUpdated` under the replacement's ID and
price, the cancel reaching the replacement; `test_paper_replace.py` asserts the four observed answers. Not
run: TC-E31 and TC-E33 (a short sale from flat), TC-E32 and TC-E35 (no new hook), TC-E34 (offline only); not
applicable: TC-E36. The offline tests stand in for the venue as alpaca-py's own tests mock its paper
endpoints, and are listed in the package README.

Paper, overnight, 2026-11-10 New York (about 22:30 to 23:00), extended-hours DAY limit buys through
alpaca-py with `raw_data=True`, logs kept outside the repo:

* A partly filled order replaced and then filled, 2 of 2 runs (NVDA, 20 shares; 18 and 16 filled first):
  the answer was the replacement `partially_filled`, `qty` 20, `filled_qty` 18 (16), `replaces` the old
  order; the old order read `replaced` with its `filled_qty` and `replaced_by`; the replacement's `fill`
  carried its own `qty` (2, 4) and the order's `filled_qty` 20; the activities' `cum_qty` were 18 (16) and
  20, `leaves_qty` 2 (4) and 0. So a replacement carries the order on: `FilledReading.CHAIN`, the reading
  the client has used, is in force as observed.
* 9 of 9 replaces: no `pending_new` or `new` for the replacement; after the old order's `replaced`, its
  first update was its last (`canceled` or `fill`).
* A PATCH on a submit answered `pending_new`, 3 of 3: accepted, answered `new`, the old order `replaced`.
* The stream's `replaced` against a read showing `replaced`, 7 samples: within 3 ms of it.
* A replace with the order's own terms: 422 "order parameters are not changed". A node does not send one:
  NautilusTrader's `Strategy` refuses to create the `ModifyOrder` ("quantity, price, and trigger were
  either None or the same as existing values", observed offline).
* A second replace of a replaced order: 422 "order already replaced", 7 of 7.

**One identity for a fill (2026-11-11).** NautilusTrader: "Stable `trade_id` values let the engine
deduplicate the same fill across both channels. If the logical fill arrives with different IDs, the engine
treats the reports as distinct" (execution `index.md`, "Overfills"); a reconciliation guide distinguishes a
snapshot that predates a fill from "A snapshot that covers the fill" (`reconciliation.md`, "Snapshot
freshness and fill corrections"); and an inferred fill's quantity is `report.filled_qty -
order.filled_qty()` (`crates/execution/src/reconciliation/orders.rs`; the same difference prices its
commission in `reconcile_order_report`, `crates/live/src/execution/manager.rs`). Offline, on a real
`LiveNode`, four cases failing before the rule and passing with it: an open order's `QueryOrder` report
showing a fill before the stream's update for that share, on the same connect and after a reconnect (FILLED 2 on one share at Alpaca,
reported by two independent readers, Muse and Antigravity), and the order's next fill still counted; the
same at start-up reconciliation while the activities lag (Antigravity); and a fill report, a stream fill
and the report's fill again from the stream, interleaved (PARTIALLY_FILLED 3 with 4 filled at Alpaca,
reported by Codex). The code review of the rule added three, each failing on a rule that judged by
quantity alone: a stream fill after a fill report for a later share, a `partial_fill` whose order shows the
count before the fill, and a late fill of an order since replaced that a report had shown; and a third
round one more, a stream fill matched to an inferred share delivered again after a reconnect. (Corrected
2026-11-11, by a second reader, Antigravity: the healing described next does not hold under the paper
node's settings. With open orders only in the open-order check, a closed order is not read again, and the
position check books the missing share to an `EXTERNAL` order; ADR 0075 replaces the model.) A known limit
stays: an inferred share whose stream update never comes is taken for the order's next fill under a new
ID unless its `filled_qty` shows it is past what the node applied. Read at the pin (`check_open_orders`,
`reconcile_open_order_reports`, `reconcile_order_report`): while Alpaca shows the order open, the
open-order check (every 10 s on the paper node, off by NautilusTrader's default) infers the missing share
once no local activity is within its threshold, so the count heals; once Alpaca shows it closed, the
engine holds a terminal report with missing fills back, "waiting for fill reports", and whether anything
before the next start-up brings them is UNVERIFIED; with the check off, it stays until then. That the
stream's `execution_id` and a fill activity's trade ID are one UUID was observed on paper (ADR 0024), not
documented. What paper sends in such a race, and whether a `partial_fill`'s order copy shows
`filled_qty` after the fill (the stream page's examples are `fill`s, which do), are UNVERIFIED.

`UNVERIFIED`, shown by no run: `order_replace_rejected`; `pending_replace` (never seen in 9 replaces); a
status on a replace other than 422; whether the regular session counts a partly filled replacement as
overnight did; a PATCH on a submit in the regular session; a fill on the old order racing a replace,
where the old order's fill applies as usual and the replacement's `rejected` becomes `OrderModifyRejected`,
not `OrderRejected`.

## Evidence

* Split from ADR 0036 on 2026-11-10 with no change of decision: the Drivers, the modify option and outcome,
  the paper rows, the `UNVERIFIED` items and the evidence lines below are ADR 0036's own, moved; the source
  lines that support both are kept in each. New: the title, the Context, the "Chosen" line, the
  Consequences and the Confirmation's opening; the release and second-reader lines are narrowed to modify,
  and the second-reader line's `dispatch.rs` now names its path.

Read 2026-11-10; "raw": fetched with `curl` and read, not summarised. NautilusTrader files at the pin,
`d53849b216e2`; alpaca-py 0.45.0 and NautilusTrader `v2.0.0rc8` as installed, read with `inspect`.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md: the order hooks, as at the
  pin but two version lines (fetched, raw; diffed). `docs/concepts/execution/policies.md`: the failure
  events; `latest` adds `OrderModifyDenied` (fetched, raw; diffed).
  https://nautilustrader.io/docs/md/latest/developer_guide/spec_exec_testing.md: group 4, the TC-E30 card,
  `ExecTester`'s setup (fetched, raw).
* `crates/adapters/binance/src/spot/execution.rs` at the pin: `modify_order`, both paths (fetched, raw).
* https://docs.alpaca.markets/us/: `reference/patchorderbyorderid-1.md` (the body, the answer, no error
  status), `docs/websocket-streaming.md` (the events) (fetched, raw).
* https://github.com/alpacahq/alpaca-py `v0.45.0`, `tests/trading/trading_client/test_order_routes.py`: the
  paper base URL mocked for orders and replaces (fetched, raw).
* Paper, 2026-11-10, logs kept outside the repo: the replace observation, 18:52 UTC, 3 runs through
  alpaca-py with `raw_data=True`.
* Second reader after landing, 2026-11-10: an independent review of the modify change found three defects,
  each reproduced through the stand-in venue and a real node, and fixed with a regression test each: a
  filled replacement whose `replaced` was missed went unreported, so its fill was lost; a replace refused on
  the HTTP answer and on the stream gave two `OrderModifyRejected`; a `replaced` delivered twice gave two
  `OrderUpdated` (whether Alpaca repeats it is UNVERIFIED). Two more independent reviews the same day added:
  a trace missed in the start-up window kept for the session; a fill on a replacement before its `replaced`
  applied to the old terms (an overfill dropped); a late cancel of the replaced order closing the
  replacement; `pending_replace` failing start-up; a replacement replaced again before its `replaced` was
  read; a refused second modify taken for a retry; answers kept for the session; a chain past the limit
  walked again each time. Each reproduced and is fixed with a test,following upstream's Binance spot
  `crates/adapters/binance/src/common/dispatch.rs` (`record_order_update`, `record_order_update_from_fill`,
  `defer_replace_cancel`). Not fixed, UNVERIFIED: whether a replacement's `filled_qty` counts the fills of
  the order it replaced. A rejected replacement after a fill now leaves the fill as the outcome, since
  NautilusTrader drops a modify rejection for a filled order (observed).
* NautilusTrader `RELEASES.md`: v2.0.0rc9, tagged 2026-11-10, adds `OrderModifyDenied` (read by the second
  reader); the pin stays rc8 until it moves through `adopt-upstream`.
* Second reader (`evidence-verifier`), 2026-11-10, on modify once: the decision stands. Corrected above, in
  ADR 0036 before the split: the documented client order ID; no source for a 403 on a replace; TC-E30's
  criterion; Binance's two paths.
* A second reader after landing, 2026-11-10: traces were cached by the Alpaca order's client order ID, so
  two orders made outside the node with an empty one shared a trace, either taking the other's root.
  Reproduced with a test and fixed: the cache is keyed by Alpaca's order ID.
* A second reader after landing, 2026-11-10: a trace that failed was skipped for a working replacement on a
  timeout, so the node went on without it, and raised from the whole report on a body alpaca-py cannot
  build. Reproduced with a test each and fixed: a failed trace, those classes included, fails the report
  for an open order, as any open order this slice cannot represent does (ADR 0030), and skips a closed one
  with a warning.
* A second reader after landing, 2026-11-10: a replacement that could not be read on `replaced` and was
  then rejected gave `OrderModifyRejected`, putting the node's order back to its old terms with nothing
  open at Alpaca, and its report was skipped. Reproduced with a test each and fixed: after `replaced` the
  replacement is adopted from its own first update, as this record has a replacement adopted by
  `replaced` or its own first update, so its rejection ends the order; and a rejected replacement whose
  old order Alpaca shows replaced is reported. A rejected replacement whose old order filled first still
  leaves the old order's report.
* Second readers of the modify work, 2026-11-10: a fill on the replaced order arriving after the
  replacement's `fill` was dropped; a replacement's `rejected` before `replaced` left the order on a
  rejected replacement's terms; a replacement replaced again before it was read could not be adopted; a
  rejected replacement the node held was not reported when its prior could not be read; a modify to a
  quantity of 0 had no outcome; a 404 on a replacement failed the report. Each reproduced with a failing
  test and fixed: the order ends when the client's own count of its fills reaches its quantity, which
  holds whichever way a replacement's `filled_qty` counts (UNVERIFIED), as Binance spot keeps a superseded
  order bound to the node's (`record_order_update_from_fill`); a rejected replacement Alpaca shows as
  having replaced the old order, or the node holds, took over; a 404 on a replacement is no report.
* Second readers of the modify work, 2026-11-10 (three independent reviews after #99, #107 and #117): a
  partly filled order whose replacement was rejected after taking over was left open, as NautilusTrader
  drops `OrderRejected` for it, and was reported rejected without its fill; a late rejection of a
  replacement a later replace had left behind ended the working order; fills across two replaces were
  all dropped; after a reconnect a fill on a replacement needed a read of the order before it, and a late
  cancel of the replaced order canceled the node's; a 404 or the 16th link ended a trace at the wrong
  order. Each reproduced with a failing test. The text above is corrected: the point fixes are replaced
  by one record per node order, after Binance spot (`crates/adapters/binance/src/common/dispatch.rs` at
  the pin, read raw: `OrderBinding`, `record_order_update`, `record_order_update_from_fill`,
  `begin_replace`, `reject_replace`, `restore_order`, `cleanup_terminal`), with a fill applied once per
  trade ID. Not taken from upstream: Binance's binding holds one current and its superseded venue IDs,
  where here a replacement not yet adopted is held too, since Alpaca's replacement can be rejected after
  `replaced`. What a replacement's quantities count was then UNVERIFIED; the next line records what
  paper showed.
* Paper, overnight, 2026-11-10 New York: a partly filled order's replacement carries the order's `qty`
  and fills (2 of 2 runs), so the reading in force is the one observed; the Confirmation above is
  corrected, and the earlier `UNVERIFIED` items on a partially filled modify and a late `new` are closed
  by it, for the overnight session.
* Corrected 2026-11-11: the Decision Outcome's reason for a 422 that Alpaca shows replaced as asked said
  "alpaca-py's own retry"; each replace is now sent once (ADR 0039), so it reads "an earlier replace took
  it".
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/execution/index.md
  ("Overfills", "Duplicate fill detection") and `docs/concepts/execution/reconciliation.md` at the same
  commit ("Snapshot freshness and fill corrections"; `OrderStatusReport` "Creates an order and infers any
  reported fill"), and `crates/live/src/execution/manager.rs` (`reconcile_order_report`), quoted in the
  Confirmation (fetched, raw, 2026-11-11). https://docs.alpaca.markets/us/docs/websocket-streaming.md: its
  `fill` examples carry the order with `filled_qty` equal to the filled shares; no `partial_fill` example
  (fetched, raw).
* Corrected 2026-11-11, by a second reader (Antigravity, round 1 on the fill-identity model): the healing
  of a lost update the Confirmation described does not hold with the open-order check reading open orders
  only, as on the paper node; reproduced on a real `LiveNode` and replaced by ADR 0075.
