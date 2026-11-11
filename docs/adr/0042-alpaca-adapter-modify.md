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
`_modify_order` sends no client order ID; `OrderUpdated` comes only with `replaced`, its values from the
replacement as the answer or a read of `replaced_by` gives it, nothing if neither does. Updates and reports
of a replacement are traced to the node's order through `replaces`, at most 16 links, from the orders
already fetched first, cached, misses too, and walked again after a restart. A 422 is `OrderModifyRejected`
unless Alpaca shows the order replaced as asked (alpaca-py's own retry); any other status leaves the outcome
unknown. `order_replace_rejected` and the `rejected` of a replacement that has not taken over are
`OrderModifyRejected` too. Denied before Alpaca: no venue order ID, a fractional quantity, a sub-penny
price, a price the order type lacks.

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

`UNVERIFIED`, shown by no run: `order_replace_rejected`; `pending_replace`; a `new` for a replacement later
than ~200 ms; a status on a replace other than 422; a modify of a partially filled order (what `qty` means
then); a fill on the old order racing a replace, where the old order's fill applies as usual and the
replacement's `rejected` becomes `OrderModifyRejected`, not `OrderRejected`.

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
